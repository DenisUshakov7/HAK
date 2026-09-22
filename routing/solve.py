"""Модель OR-Tools (VRPTW) для распределения заявок между инженерами.

Депо (точка 0) — общий стартовый офис. Возврат в депо не требуется по ТЗ,
поэтому дуги "в депо" обнуляются по расстоянию/дороге, но не по времени
выполнения последней заявки (иначе можно было бы формально успеть
"назначить" визит после конца смены — время сервиса на узле должно
учитываться до конца, только сама обратная дорога бесплатна)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from routing.distance_matrix import VEHICLE_TO_PROFILE, MatrixResult
from routing.models import Engineer, Order, parse_hhmm

logger = logging.getLogger(__name__)

DISTANCE_SCALE = 1000  # км -> целые единицы (метры) для стоимости дуг OR-Tools
# Фиксированная стоимость за инженера должна перевешивать любой правдоподобный
# суммарный пробег (десятки км => десятки тысяч единиц при DISTANCE_SCALE=1000),
# чтобы solver предпочитал меньше инженеров, а не меньше километров.
FIXED_COST_PER_ENGINEER = 1_000_000
# Штраф за неназначенную заявку должен быть больше стоимости ещё одного
# инженера, иначе solver будет пропускать заявки вместо того, чтобы
# задействовать исполнителя. Штрафы различаются по типу работ
# (Авария > Подключение/Дозаказ > Локальные работы): при нехватке
# исполнителей первой остаётся без назначения наименее приоритетная заявка.
# Ключ — required_skill: «Подключение» и «Дозаказ» в данных не различаются,
# «Локальные работы» приняты за аналог «Ремонта» (допущение, см. README).
UNASSIGNED_PENALTY_BY_SKILL = {
    "Аварийные работы": 70_000_000,
    "Работы на подключение и дозаказы": 50_000_000,
    "Локальные работы": 30_000_000,
}
DEFAULT_UNASSIGNED_PENALTY = 50_000_000  # навык вне таблицы — не должно происходить на текущих данных


def _unassigned_penalty(order: Order) -> int:
    if order.required_skill is not None and order.required_skill not in UNASSIGNED_PENALTY_BY_SKILL:
        # Неизвестный навык получает запасной штраф — предупреждаем об этом.
        logger.warning(
            "Навык %r отсутствует в UNASSIGNED_PENALTY_BY_SKILL — заявка %r "
            "получает запасной приоритет DEFAULT_UNASSIGNED_PENALTY (%d)",
            order.required_skill, order.id, DEFAULT_UNASSIGNED_PENALTY,
        )
    return UNASSIGNED_PENALTY_BY_SKILL.get(order.required_skill, DEFAULT_UNASSIGNED_PENALTY)
# Горизонт времени — минуты от полуночи в пределах одних суток (все смены и
# окна заявок заданы как HH:MM одного дня, многодневного планирования нет).
HORIZON_MIN = 24 * 60
# Слэк — максимально допустимое ожидание между узлами: инженер может выйти
# в срок по расписанию, но прибыть на заявку раньше открытия её окна и
# подождать. Ограничен тем же горизонтом суток, а не отдельной константой.
SLACK_MAX = 24 * 60
DEFAULT_TIME_LIMIT_SECONDS = 10
# Недостижимая дуга (ORS вернул null) хранится как float("inf"), а round(inf)
# падает. В callbacks вместо inf отдаём конечный штраф: время заведомо
# выходит за HORIZON_MIN, поэтому solver такую дугу не выберет.
UNREACHABLE_TIME_MIN = HORIZON_MIN * 10
UNREACHABLE_DISTANCE_SCALED = FIXED_COST_PER_ENGINEER * 10_000


@dataclass(frozen=True)
class StopResult:
    order_index: int
    arrival_min: int
    travel_min: int
    distance_km: float


@dataclass(frozen=True)
class VehicleRoute:
    engineer_index: int
    stops: list[StopResult]
    total_distance_km: float


@dataclass(frozen=True)
class SolveResult:
    routes: list[VehicleRoute]
    unassigned_order_indices: list[int]


def _allowed_engineers(order: Order, engineers: list[Engineer]) -> list[int]:
    allowed = []
    for idx, engineer in enumerate(engineers):
        if order.required_skill not in engineer.skills:
            continue
        if order.required_transport is not None and order.required_transport != engineer.vehicle:
            continue
        allowed.append(idx)
    return allowed


def solve(
    orders: list[Order],
    engineers: list[Engineer],
    matrices: dict[str, MatrixResult],
    time_limit_seconds: int = DEFAULT_TIME_LIMIT_SECONDS,
) -> SolveResult:
    num_orders = len(orders)
    num_points = num_orders + 1  # 0 = депо, 1..N = заявки
    num_vehicles = len(engineers)

    manager = pywrapcp.RoutingIndexManager(num_points, num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    profile_by_vehicle = [VEHICLE_TO_PROFILE[e.vehicle] for e in engineers]
    service_min = [0] + [o.duration_min for o in orders]

    distance_callback_by_profile: dict[str, int] = {}
    time_callback_by_profile: dict[str, int] = {}

    for profile, matrix in matrices.items():
        def make_distance_callback(matrix=matrix):
            def _callback(from_index, to_index):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                if to_node == 0:
                    return 0  # открытый маршрут: обратная дорога не считается
                value = matrix.distance_km[(from_node, to_node)]
                if not math.isfinite(value):
                    return UNREACHABLE_DISTANCE_SCALED
                return round(value * DISTANCE_SCALE)

            return _callback

        def make_time_callback(matrix=matrix):
            def _callback(from_index, to_index):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                if to_node == 0:
                    # Обратная дорога не считается, но время выполнения ПОСЛЕДНЕЙ
                    # заявки — всё ещё должно попасть в смену.
                    return service_min[from_node]
                travel = matrix.duration_min[(from_node, to_node)]
                if not math.isfinite(travel):
                    return UNREACHABLE_TIME_MIN
                return round(travel) + service_min[from_node]

            return _callback

        distance_callback_by_profile[profile] = routing.RegisterTransitCallback(
            make_distance_callback()
        )
        time_callback_by_profile[profile] = routing.RegisterTransitCallback(make_time_callback())

    time_transit_indices = [
        time_callback_by_profile[profile_by_vehicle[v]] for v in range(num_vehicles)
    ]
    routing.AddDimensionWithVehicleTransits(time_transit_indices, SLACK_MAX, HORIZON_MIN, False, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")

    for v, engineer in enumerate(engineers):
        routing.SetArcCostEvaluatorOfVehicle(distance_callback_by_profile[profile_by_vehicle[v]], v)
        routing.SetFixedCostOfVehicle(FIXED_COST_PER_ENGINEER, v)
        shift_start = parse_hhmm(engineer.shift_start)
        shift_end = parse_hhmm(engineer.shift_end)
        time_dimension.CumulVar(routing.Start(v)).SetRange(shift_start, shift_start)
        time_dimension.CumulVar(routing.End(v)).SetRange(0, shift_end)

    for order_idx, order in enumerate(orders):
        node = order_idx + 1
        index = manager.NodeToIndex(node)
        allowed = _allowed_engineers(order, engineers)
        # SetAllowedVehiclesForIndex не работает в этой сборке ortools (SWIG
        # не принимает список), поэтому сужаем домен VehicleVar напрямую.
        # -1 («не в маршруте») оставляем: заявка необязательная (AddDisjunction).
        routing.VehicleVar(index).SetValues(allowed + [-1])
        window_start = parse_hhmm(order.window_start)
        window_end = parse_hhmm(order.window_end)
        time_dimension.CumulVar(index).SetRange(window_start, window_end)
        routing.AddDisjunction([index], _unassigned_penalty(order))

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.FromSeconds(time_limit_seconds)

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        raise RuntimeError("OR-Tools не нашёл решения (даже с учётом необязательных заявок)")

    visited_nodes: set[int] = set()
    routes: list[VehicleRoute] = []
    for v in range(num_vehicles):
        profile = profile_by_vehicle[v]
        matrix = matrices[profile]
        index = routing.Start(v)
        stops: list[StopResult] = []
        prev_node = 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                arrival_min = solution.Value(time_dimension.CumulVar(index))
                raw_travel = matrix.duration_min[(prev_node, node)]
                # Страховка: недостижимая дуга не должна попасть в маршрут;
                # если попала — лучше упасть, чем выдать невозможный план.
                if not math.isfinite(raw_travel):
                    raise RuntimeError(
                        f"OR-Tools использовал недостижимую дугу между точками "
                        f"{prev_node} и {node} для инженера {engineers[v].id} — "
                        f"решение отклонено, а не отдано как есть"
                    )
                travel_min = round(raw_travel)
                distance_km = matrix.distance_km[(prev_node, node)]
                stops.append(
                    StopResult(
                        order_index=node - 1,
                        arrival_min=arrival_min,
                        travel_min=travel_min,
                        distance_km=distance_km,
                    )
                )
                visited_nodes.add(node)
                prev_node = node
            index = solution.Value(routing.NextVar(index))
        if stops:
            routes.append(
                VehicleRoute(
                    engineer_index=v,
                    stops=stops,
                    total_distance_km=sum(s.distance_km for s in stops),
                )
            )

    unassigned_order_indices = [idx for idx in range(num_orders) if (idx + 1) not in visited_nodes]

    return SolveResult(routes=routes, unassigned_order_indices=unassigned_order_indices)
