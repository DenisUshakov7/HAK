"""Базовый вариант распределения без оптимизации (ТЗ §2.3, буквально):
заявки обрабатываются по порядку поступления и назначаются первому по
порядку во входных данных доступному инженеру, который удовлетворяет
обязательным ограничениям; порядок посещения соответствует порядку
назначения. Глобальная оптимизация не выполняется — используется для
сравнения эффективности с оптимизированным (routing/solve.py) планом."""
from __future__ import annotations

import math

from routing.distance_matrix import VEHICLE_TO_PROFILE, MatrixResult
from routing.models import Engineer, Order, parse_hhmm
from routing.solve import SolveResult, StopResult, VehicleRoute


def solve_baseline(
    orders: list[Order], engineers: list[Engineer], matrices: dict[str, MatrixResult]
) -> SolveResult:
    last_node = [0] * len(engineers)
    current_time = [parse_hhmm(e.shift_start) for e in engineers]
    stops_by_engineer: list[list[StopResult]] = [[] for _ in engineers]
    assigned: set[int] = set()

    for order_idx, order in enumerate(orders):
        node = order_idx + 1
        for eng_idx, engineer in enumerate(engineers):
            if order.required_skill not in engineer.skills:
                continue
            if order.required_transport is not None and order.required_transport != engineer.vehicle:
                continue

            matrix = matrices[VEHICLE_TO_PROFILE[engineer.vehicle]]
            raw_travel = matrix.duration_min[(last_node[eng_idx], node)]
            if not math.isfinite(raw_travel):
                # Дуга недостижима (ORS вернул null): инженер не доедет,
                # пробуем следующего по очереди.
                continue
            travel_min = round(raw_travel)
            arrival = current_time[eng_idx] + travel_min
            window_start = parse_hhmm(order.window_start)
            window_end = parse_hhmm(order.window_end)
            start_of_work = max(arrival, window_start)
            # Окно ограничивает только начало работы (ТЗ §2.2), завершение —
            # только смена. Так же, как в routing/solve.py.
            if start_of_work > window_end:
                continue
            if start_of_work + order.duration_min > parse_hhmm(engineer.shift_end):
                continue

            stops_by_engineer[eng_idx].append(
                StopResult(
                    order_index=order_idx,
                    arrival_min=start_of_work,
                    travel_min=travel_min,
                    distance_km=matrix.distance_km[(last_node[eng_idx], node)],
                )
            )
            last_node[eng_idx] = node
            current_time[eng_idx] = start_of_work + order.duration_min
            assigned.add(order_idx)
            break

    routes = [
        VehicleRoute(
            engineer_index=eng_idx,
            stops=stops,
            total_distance_km=sum(s.distance_km for s in stops),
        )
        for eng_idx, stops in enumerate(stops_by_engineer)
        if stops
    ]
    unassigned_order_indices = [i for i in range(len(orders)) if i not in assigned]

    return SolveResult(routes=routes, unassigned_order_indices=unassigned_order_indices)


def explain_baseline_assigned(order: Order, engineer: Engineer, engineers: list[Engineer]) -> str:
    # engineers не используется: сигнатура совпадает с explain_assigned,
    # чтобы build_plan_output вызывал обе функции одинаково.
    return (
        "Первый подходящий по очереди инженер — базовый вариант без "
        "оптимизации маршрута (заявки и инженеры перебираются в порядке "
        "входных данных, без учёта минимизации пробега)"
    )
