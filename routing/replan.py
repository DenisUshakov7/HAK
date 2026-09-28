"""Точечный патч плана распределения по одному событию перепланирования
(ТЗ §2.1.6/§2.4/§2.4.2). Работает поверх уже посчитанного
assignment.json, не пересчитывает план с нуля — новая/осиротевшая
заявка вставляется в самую дешёвую по добавленному пробегу открытую
позицию среди всех подходящих инженеров."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from routing.distance_matrix import DEFAULT_CACHE_PATH, DistanceMatrixBuilder, VEHICLE_TO_PROFILE, MatrixResult
from routing.explain import explain_unassigned
from routing.models import Engineer, Order, format_hhmm, load_engineers, load_office_coords, load_orders, parse_hhmm

logger = logging.getLogger(__name__)

URGENT_PLACED_REASON = (
    "Срочная заявка — поставлена на самое раннее допустимое время среди подходящих инженеров"
)

# engineer_id -> [(from_min, until_min), ...]: окна, в которых инженер не
# начинает работу (until_min = 1440 для «до конца смены»).
Blackouts = dict[str, list[tuple[int, int]]]


def _push_out_of_blackouts(time_min: int, windows) -> int:
    """Если time_min попадает в окно [from, until), возвращает until (и так
    далее для смежных окон); иначе time_min без изменений."""
    moved = True
    while moved:
        moved = False
        for start, end in windows:
            if start <= time_min < end:
                time_min = end
                moved = True
    return time_min


@dataclass(frozen=True)
class ReplanEvent:
    type: str
    event_time: str
    order_id: str | None = None
    engineer_id: str | None = None
    new_order: Order | None = None
    unavailable_from: str | None = None
    unavailable_until: str | None = None


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_bound(value, label: str) -> int:
    match = _HHMM.match(value.strip()) if isinstance(value, str) else None
    if match is None or int(match.group(1)) > 24 or int(match.group(2)) > 59:
        raise ValueError(f"{label}: ожидается формат ЧЧ:ММ, получено {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def blackout_for_event(event: ReplanEvent) -> tuple[str, int, int] | None:
    """Окно недоступности (engineer_id, from_min, until_min) из события
    engineer_unavailable; для остальных типов None. from по умолчанию —
    время события и не может быть раньше него (прошлое не меняем); until
    по умолчанию — конец суток («до конца смены»)."""
    if event.type != "engineer_unavailable":
        return None
    event_min = parse_hhmm(event.event_time)
    from_min = event_min
    if event.unavailable_from:
        from_min = max(_parse_bound(event.unavailable_from, "Недоступен с"), event_min)
    until_min = _parse_bound(event.unavailable_until, "Недоступен до") if event.unavailable_until else 1440
    if until_min <= from_min:
        raise ValueError(
            f"Конец недоступности ({format_hhmm(until_min)}) должен быть позже её "
            f"начала ({format_hhmm(from_min)})"
        )
    return event.engineer_id, from_min, until_min


def load_event(path: Path) -> ReplanEvent:
    raw = json.loads(path.read_text(encoding="utf-8"))
    event_type = raw["type"]

    if event_type == "new_urgent_order":
        new_order = Order(
            id=raw["id"],
            lat=raw["lat"],
            lon=raw["lon"],
            duration_min=raw["duration_min"],
            window_start=raw["window_start"],
            window_end=raw["window_end"],
            priority=raw["priority"],
            required_skill=raw["required_skill"],
            required_transport=raw.get("required_transport"),
        )
        return ReplanEvent(type=event_type, event_time=raw["event_time"], new_order=new_order)

    if event_type == "cancel_order":
        return ReplanEvent(type=event_type, event_time=raw["event_time"], order_id=raw["order_id"])

    if event_type == "engineer_unavailable":
        event = ReplanEvent(
            type=event_type,
            event_time=raw["event_time"],
            engineer_id=raw["engineer_id"],
            unavailable_from=raw.get("from"),
            unavailable_until=raw.get("until"),
        )
        blackout_for_event(event)  # валидация интервала
        return event

    raise ValueError(f"Неизвестный тип события: {event_type!r}")


@dataclass
class StopTiming:
    order_id: str
    arrival_min: int
    travel_min: int
    distance_km: float


def _simulate_tail(
    engineer: Engineer,
    start_point_id: str,
    start_time: int,
    order_id_sequence: list[str],
    orders_by_id: dict[str, Order],
    matrix: MatrixResult,
    point_index: dict[str, int],
    blackouts=(),
) -> list[StopTiming] | None:
    """Симулирует визит order_id_sequence начиная с start_point_id в
    start_time. Окно ограничивает только начало работы (ТЗ §2.2, та же
    семантика, что в routing/solve.py и routing/baseline.py), смена
    ограничивает завершение. blackouts — окна недоступности инженера: старт
    работы внутри окна сдвигается на его конец. None, если хоть одна
    остановка не влезает."""
    current_point = start_point_id
    current_time = start_time
    result: list[StopTiming] = []
    for order_id in order_id_sequence:
        order = orders_by_id[order_id]
        travel_min = round(matrix.duration_min[(point_index[current_point], point_index[order_id])])
        arrival = current_time + travel_min
        start_of_work = _push_out_of_blackouts(max(arrival, parse_hhmm(order.window_start)), blackouts)
        if start_of_work > parse_hhmm(order.window_end):
            return None
        if start_of_work + order.duration_min > parse_hhmm(engineer.shift_end):
            return None
        distance_km = matrix.distance_km[(point_index[current_point], point_index[order_id])]
        result.append(StopTiming(order_id, start_of_work, travel_min, distance_km))
        current_point = order_id
        current_time = start_of_work + order.duration_min
    return result


def _find_cheapest_insertion(
    new_order: Order,
    engineers: list[Engineer],
    routes: dict[str, list[str]],
    departure_after: dict[str, list[int]],
    frozen_counts: dict[str, int],
    orders_by_id: dict[str, Order],
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
    blackouts: Blackouts | None = None,
) -> tuple[float, str, int] | None:
    """Ищет (стоимость, engineer_id, позиция) среди всех подходящих
    инженеров и всех открытых (не замороженных) позиций их маршрутов.
    Обычная заявка — с минимальной добавленной дистанцией (стандартная
    VRP-формула вставки d(prev,new)+d(new,next)-d(prev,next)); полная
    пересимуляция хвоста нужна только для проверки допустимости. Срочная —
    с самым ранним временем начала работы, при равенстве — с минимальной
    дистанцией (организаторы допускают для аварии реакцию за 1–2 часа).
    blackouts уже включает окно [0, event_time) для каждого инженера
    (см. apply_event), поэтому заявка не получит время раньше самого
    события. None, если нигде не влезает."""
    earliest_first = new_order.priority == "Срочная"
    best_key: tuple | None = None
    best: tuple[float, str, int] | None = None
    for engineer in engineers:
        if new_order.required_skill not in engineer.skills:
            continue
        if new_order.required_transport is not None and new_order.required_transport != engineer.vehicle:
            continue

        matrix = matrices[VEHICLE_TO_PROFILE[engineer.vehicle]]
        windows = (blackouts or {}).get(engineer.id, ())
        route = routes[engineer.id]
        frozen = frozen_counts[engineer.id]

        for pos in range(frozen, len(route) + 1):
            prev_id = route[pos - 1] if pos > 0 else "__office__"
            next_id = route[pos] if pos < len(route) else None

            d_prev_new = matrix.distance_km[(point_index[prev_id], point_index[new_order.id])]
            if next_id is not None:
                d_new_next = matrix.distance_km[(point_index[new_order.id], point_index[next_id])]
                d_prev_next = matrix.distance_km[(point_index[prev_id], point_index[next_id])]
                cost = d_prev_new + d_new_next - d_prev_next
            else:
                cost = d_prev_new

            if not earliest_first and best is not None and cost >= best[0]:
                continue

            start_time = departure_after[engineer.id][pos]
            new_tail = [new_order.id] + route[pos:]
            timings = _simulate_tail(engineer, prev_id, start_time, new_tail, orders_by_id, matrix, point_index, windows)
            if timings is None:
                continue

            key = (timings[0].arrival_min, cost) if earliest_first else (cost,)
            if best_key is None or key < best_key:
                best_key = key
                best = (cost, engineer.id, pos)

    return best


def _find_insertion_by_evicting_normal_work(
    new_order: Order,
    engineers: list[Engineer],
    state: _PlanState,
    orders_by_id: dict[str, Order],
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> tuple[str, int, str] | None:
    """Если срочная заявка не влезает без изменений (ТЗ §2.4.1: у срочной
    приоритет), снимает с маршрута одну ещё не начатую заявку с приоритетом
    «Обычная» и ставит срочную на её место. Только прямая замена, без
    каскадов и без сравнения стоимости. Возвращает (engineer_id, позиция в
    хвосте, evicted_order_id) или None."""
    for engineer in engineers:
        if new_order.required_skill not in engineer.skills:
            continue
        if new_order.required_transport is not None and new_order.required_transport != engineer.vehicle:
            continue

        matrix = matrices[VEHICLE_TO_PROFILE[engineer.vehicle]]
        windows = state.blackouts.get(engineer.id, ())
        frozen = state.frozen_counts[engineer.id]
        tail = state.routes[engineer.id][frozen:]
        start_point = state.routes[engineer.id][frozen - 1] if frozen > 0 else "__office__"
        start_time = state.departure_after[engineer.id][frozen]

        for local_idx, evict_id in enumerate(tail):
            if orders_by_id[evict_id].priority == "Срочная":
                continue  # вытесняем только «Обычную», не другую срочную
            trial_tail = tail[:local_idx] + [new_order.id] + tail[local_idx + 1 :]
            if _simulate_tail(
                engineer, start_point, start_time, trial_tail, orders_by_id, matrix, point_index, windows
            ) is not None:
                return engineer.id, frozen + local_idx, evict_id

    return None


@dataclass
class ChangeRecord:
    kind: str
    order_id: str
    engineer_id: str | None = None
    from_engineer_id: str | None = None
    arrival_min: int | None = None
    reason: str | None = None


@dataclass
class _PlanState:
    routes: dict[str, list[str]]
    original_reasons: dict[str, str]
    unassigned_reasons: dict[str, str]
    departure_after: dict[str, list[int]]
    frozen_counts: dict[str, int]
    blackouts: Blackouts = field(default_factory=dict)
    # Инженеры, у которых в этом apply_event вставили или сняли остановку;
    # остальные маршруты берутся из исходного плана как есть.
    touched: set[str] = field(default_factory=set)
    event_time_min: int = 0  # момент события: раньше него никто не выезжает


def _load_current_state(
    assignment: dict,
    orders_by_id: dict[str, Order],
    engineers_by_id: dict[str, Engineer],
    event_time_min: int,
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, str], dict[str, list[int]], dict[str, int]]:
    routes: dict[str, list[str]] = {}
    original_reasons: dict[str, str] = {}
    departure_after: dict[str, list[int]] = {}
    frozen_counts: dict[str, int] = {}

    for route in assignment["routes"]:
        engineer_id = route["engineer_id"]
        stops = route["stops"]
        routes[engineer_id] = [s["order_id"] for s in stops]

        engineer = engineers_by_id.get(engineer_id)
        if engineer is None:
            raise ValueError(
                f"Инженер {engineer_id!r} есть в assignment.json, но отсутствует "
                f"в engineers.json — assignment.json устарел, перегенерируйте его "
                f"(python -m routing.run_distribute)"
            )
        deps = [parse_hhmm(engineer.shift_start)]
        frozen = 0
        frozen_done = False
        for s in stops:
            original_reasons[s["order_id"]] = s["reason"]
            arrival_min = parse_hhmm(s["arrival"])
            order = orders_by_id.get(s["order_id"])
            if order is None:
                raise ValueError(
                    f"Заявка {s['order_id']!r} есть в assignment.json, но отсутствует "
                    f"в orders.json — assignment.json устарел, перегенерируйте его "
                    f"(python -m routing.run_distribute)"
                )
            deps.append(arrival_min + order.duration_min)
            if not frozen_done:
                if arrival_min < event_time_min:
                    frozen += 1
                else:
                    frozen_done = True
        departure_after[engineer_id] = deps
        frozen_counts[engineer_id] = frozen

    for engineer_id, engineer in engineers_by_id.items():
        routes.setdefault(engineer_id, [])
        departure_after.setdefault(engineer_id, [parse_hhmm(engineer.shift_start)])
        frozen_counts.setdefault(engineer_id, 0)

    unassigned_reasons = {u["order_id"]: u["reason"] for u in assignment["unassigned"]}

    return routes, original_reasons, unassigned_reasons, departure_after, frozen_counts


def _resimulate_tail(
    engineer: Engineer,
    state: _PlanState,
    orders_by_id: dict[str, Order],
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> list[StopTiming]:
    """Пересчитывает только хвост маршрута — остановки после замороженного
    префикса. Префикс не пересимулируется: точка и время, откуда идёт
    хвост (state.departure_after[engineer.id][frozen]), взяты из исходного
    assignment.json и не меняются."""
    frozen = state.frozen_counts[engineer.id]
    route = state.routes[engineer.id]
    tail = route[frozen:]
    start_point = route[frozen - 1] if frozen > 0 else "__office__"
    start_time = state.departure_after[engineer.id][frozen]
    matrix = matrices[VEHICLE_TO_PROFILE[engineer.vehicle]]
    timings = _simulate_tail(
        engineer, start_point, start_time, tail, orders_by_id, matrix, point_index,
        state.blackouts.get(engineer.id, ()),
    )
    if timings is None:
        raise ValueError(
            f"Маршрут инженера {engineer.id!r} перестал быть допустимым после "
            f"патча при пересчёте по текущей матрице расстояний — вероятно, она "
            f"построена из другого источника (сеть/кэш), чем assignment.json. "
            f"Проверьте ORS_API_KEY/сеть или перегенерируйте assignment.json "
            f"в тех же условиях (python -m routing.run_distribute)"
        )
    state.departure_after[engineer.id] = (
        state.departure_after[engineer.id][: frozen + 1]
        + [t.arrival_min + orders_by_id[t.order_id].duration_min for t in timings]
    )
    return timings


def _explain_unassigned(
    order: Order,
    engineers: list[Engineer],
    state: _PlanState,
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> str:
    """Причина отказа с учётом момента события. Расчёт «по дороге из офиса»
    здесь не применяется: при перепланировании инженеры уже не в офисе,
    могут быть недоступны, и такая причина была бы неверной."""
    return explain_unassigned(order, engineers, None, state.event_time_min)


def _reinsert_orphan(
    order_id: str,
    from_engineer_id: str,
    engineers: list[Engineer],
    engineers_by_id: dict[str, Engineer],
    state: _PlanState,
    orders_by_id: dict[str, Order],
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> list[ChangeRecord]:
    """Ставит осиротевшую (снятую с чьего-то маршрута) заявку в самую
    дешёвую допустимую позицию среди engineers — для заявок недоступного
    инженера и для заявки, вытесненной срочной."""
    order = orders_by_id[order_id]
    result = _find_cheapest_insertion(
        order, engineers, state.routes, state.departure_after, state.frozen_counts,
        orders_by_id, matrices, point_index, blackouts=state.blackouts,
    )
    if result is None:
        reason = _explain_unassigned(order, engineers, state, matrices, point_index)
        state.unassigned_reasons[order_id] = reason
        return [ChangeRecord(kind="newly_unassigned", order_id=order_id, reason=reason)]

    _, new_engineer_id, pos = result
    state.routes[new_engineer_id].insert(pos, order_id)
    state.touched.add(new_engineer_id)
    new_engineer = engineers_by_id[new_engineer_id]
    timings = _resimulate_tail(new_engineer, state, orders_by_id, matrices, point_index)
    arrival_min = next(t.arrival_min for t in timings if t.order_id == order_id)
    return [
        ChangeRecord(
            kind="reassigned", order_id=order_id, engineer_id=new_engineer_id,
            from_engineer_id=from_engineer_id, arrival_min=arrival_min,
            reason=URGENT_PLACED_REASON if order.priority == "Срочная" else None,
        )
    ]


def _apply_new_urgent_order(
    event: ReplanEvent,
    engineers: list[Engineer],
    engineers_by_id: dict[str, Engineer],
    orders_by_id: dict[str, Order],
    state: _PlanState,
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> list[ChangeRecord]:
    new_order = event.new_order
    result = _find_cheapest_insertion(
        new_order, engineers, state.routes, state.departure_after, state.frozen_counts,
        orders_by_id, matrices, point_index, blackouts=state.blackouts,
    )

    changes: list[ChangeRecord] = []
    evicted_order_id: str | None = None
    evicted_from_engineer_id: str | None = None

    if result is None and new_order.priority == "Срочная":
        found = _find_insertion_by_evicting_normal_work(
            new_order, engineers, state, orders_by_id, matrices, point_index,
        )
        if found is not None:
            engineer_id, pos, evicted_order_id = found
            evicted_from_engineer_id = engineer_id
            state.routes[engineer_id].pop(pos)
            result = (0.0, engineer_id, pos)

    if result is None:
        reason = _explain_unassigned(new_order, engineers, state, matrices, point_index)
        state.unassigned_reasons[new_order.id] = reason
        changes.append(ChangeRecord(kind="newly_unassigned", order_id=new_order.id, reason=reason))
        return changes

    _, engineer_id, pos = result
    state.routes[engineer_id].insert(pos, new_order.id)
    state.touched.add(engineer_id)
    engineer = engineers_by_id[engineer_id]
    timings = _resimulate_tail(engineer, state, orders_by_id, matrices, point_index)
    arrival_min = next(t.arrival_min for t in timings if t.order_id == new_order.id)
    # Срочная — не минимизация пробега, поэтому причина задаётся явно.
    if evicted_order_id is not None:
        placed_reason = (
            f"Срочная заявка — вытеснила ещё не начатую заявку {evicted_order_id!r} "
            f"с приоритетом «Обычная», другого места не нашлось"
        )
    elif new_order.priority == "Срочная":
        placed_reason = URGENT_PLACED_REASON
    else:
        placed_reason = None
    changes.append(
        ChangeRecord(
            kind="newly_assigned", order_id=new_order.id, engineer_id=engineer_id,
            arrival_min=arrival_min, reason=placed_reason,
        )
    )

    if evicted_order_id is not None:
        changes.extend(
            _reinsert_orphan(
                evicted_order_id, evicted_from_engineer_id, engineers, engineers_by_id, state,
                orders_by_id, matrices, point_index,
            )
        )

    return changes


def _apply_cancel_order(
    event: ReplanEvent,
    orders_by_id: dict[str, Order],
    engineers_by_id: dict[str, Engineer],
    state: _PlanState,
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> list[str]:
    order_id = event.order_id

    if order_id in state.unassigned_reasons:
        del state.unassigned_reasons[order_id]
        return [order_id]

    owner_engineer_id = None
    position = None
    for engineer_id, stop_ids in state.routes.items():
        if order_id in stop_ids:
            owner_engineer_id = engineer_id
            position = stop_ids.index(order_id)
            break

    if owner_engineer_id is None:
        raise ValueError(f"Заявка {order_id!r} не найдена в текущем плане — нечего отменять")

    if position < state.frozen_counts[owner_engineer_id]:
        raise ValueError(
            f"Заявка {order_id!r} уже была выполнена до времени события "
            f"{event.event_time} — нельзя отменить то, что уже произошло"
        )

    state.routes[owner_engineer_id].pop(position)
    state.original_reasons.pop(order_id, None)
    state.touched.add(owner_engineer_id)

    engineer = engineers_by_id[owner_engineer_id]
    _resimulate_tail(engineer, state, orders_by_id, matrices, point_index)

    return [order_id]


def _apply_engineer_unavailable(
    event: ReplanEvent,
    engineers: list[Engineer],
    engineers_by_id: dict[str, Engineer],
    orders_by_id: dict[str, Order],
    state: _PlanState,
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
) -> list[ChangeRecord]:
    engineer_id = event.engineer_id
    if engineer_id not in engineers_by_id:
        raise ValueError(f"Инженер {engineer_id!r} не найден — нечего делать недоступным")

    _, from_min, until_min = blackout_for_event(event)
    state.blackouts.setdefault(engineer_id, []).append((from_min, until_min))

    unavailable_route = state.routes[engineer_id]
    frozen = state.frozen_counts[engineer_id]
    starts = {
        stop_id: state.departure_after[engineer_id][index + 1] - orders_by_id[stop_id].duration_min
        for index, stop_id in enumerate(unavailable_route)
    }
    orphaned_ids = [
        stop_id for index, stop_id in enumerate(unavailable_route)
        if index >= frozen and from_min <= starts[stop_id] < until_min
    ]
    kept_route = [stop_id for stop_id in unavailable_route if stop_id not in orphaned_ids]
    state.routes[engineer_id] = kept_route
    if kept_route != unavailable_route:
        # Хвост пересчитываем, только если что-то сняли: если окно не задело
        # ни одной остановки, маршрут остаётся как есть.
        state.touched.add(engineer_id)
        engineer = engineers_by_id[engineer_id]
        _resimulate_tail(engineer, state, orders_by_id, matrices, point_index)

    if until_min == 1440:
        remaining_engineers = [e for e in engineers if e.id != engineer_id]
    else:
        remaining_engineers = list(engineers)
    remaining_engineers_by_id = {e.id: e for e in remaining_engineers}

    def sort_key(order_id: str) -> tuple[int, int]:
        is_urgent = orders_by_id[order_id].priority == "Срочная"
        return (0 if is_urgent else 1, unavailable_route.index(order_id))

    changes: list[ChangeRecord] = []
    for order_id in sorted(orphaned_ids, key=sort_key):
        changes.extend(
            _reinsert_orphan(
                order_id, engineer_id, remaining_engineers, remaining_engineers_by_id, state,
                orders_by_id, matrices, point_index,
            )
        )

    return changes


def _build_diff(
    changes: list[ChangeRecord],
    cancelled: list[str],
    before_routes: dict[str, list[str]],
    after_routes: dict[str, list[str]],
) -> dict:
    newly_assigned = [
        {"order_id": c.order_id, "engineer_id": c.engineer_id, "arrival": format_hhmm(c.arrival_min)}
        for c in changes if c.kind == "newly_assigned"
    ]
    newly_unassigned = [
        {"order_id": c.order_id, "reason": c.reason}
        for c in changes if c.kind == "newly_unassigned"
    ]
    reassigned = [
        {
            "order_id": c.order_id,
            "from_engineer_id": c.from_engineer_id,
            "to_engineer_id": c.engineer_id,
            "arrival": format_hhmm(c.arrival_min),
        }
        for c in changes if c.kind == "reassigned"
    ]
    all_engineer_ids = sorted(set(before_routes) | set(after_routes))
    routes_changed = [
        {
            "engineer_id": eid,
            "before_stops": before_routes.get(eid, []),
            "after_stops": after_routes.get(eid, []),
        }
        for eid in all_engineer_ids
        if before_routes.get(eid, []) != after_routes.get(eid, [])
    ]
    return {
        "newly_assigned": newly_assigned,
        "newly_unassigned": newly_unassigned,
        "reassigned": reassigned,
        "cancelled": cancelled,
        "routes_changed": routes_changed,
    }


def _build_replan_plan(
    state: _PlanState,
    orders_by_id: dict[str, Order],
    engineers_by_id: dict[str, Engineer],
    changes: list[ChangeRecord],
    matrices: dict[str, MatrixResult],
    point_index: dict[str, int],
    original_routes_by_engineer: dict[str, dict],
) -> dict:
    """Собирает итоговый план. Замороженный префикс берётся из
    assignment.json как есть. Хвост тоже берётся как есть, если маршрут не
    менялся (state.touched): пересчёт нетронутого маршрута по другой
    матрице расстояний (сеть или кэш) мог бы счесть его недопустимым."""
    newly_placed_ids = {c.order_id for c in changes if c.kind in ("newly_assigned", "reassigned")}
    # По умолчанию причина — минимальный добавленный пробег; при вытеснении
    # причина задана явно в ChangeRecord.
    custom_placed_reasons = {c.order_id: c.reason for c in changes if c.reason and c.order_id in newly_placed_ids}

    routes_out = []
    engineers_used = 0
    total_distance_km = 0.0
    orders_assigned = 0
    for engineer_id, stop_ids in state.routes.items():
        if not stop_ids:
            continue
        engineer = engineers_by_id[engineer_id]
        frozen = state.frozen_counts[engineer_id]

        original_route = original_routes_by_engineer.get(engineer_id)
        original_stops = original_route["stops"] if original_route is not None else []
        # Замороженный префикс не меняется.
        frozen_stops_out = original_stops[:frozen]

        tail_ids = stop_ids[frozen:]
        if not tail_ids:
            tail_stops_out = []
        elif engineer_id in state.touched:
            timings = _resimulate_tail(engineer, state, orders_by_id, matrices, point_index)
            tail_stops_out = [
                {
                    "order_id": t.order_id,
                    "arrival": format_hhmm(t.arrival_min),
                    "travel_min": t.travel_min,
                    "distance_km": round(t.distance_km, 2),
                    "reason": (
                        custom_placed_reasons.get(
                            t.order_id,
                            "Вставлена при перепланировании — минимальный добавленный "
                            "пробег среди подходящих инженеров",
                        )
                        if t.order_id in newly_placed_ids
                        else state.original_reasons.get(t.order_id, "")
                    ),
                }
                for t in timings
            ]
        else:
            # Хвост не менялся — берём исходные остановки как есть.
            tail_stops_out = original_stops[frozen : frozen + len(tail_ids)]

        stops_out = frozen_stops_out + tail_stops_out
        route_distance = sum(s["distance_km"] for s in stops_out)

        engineers_used += 1
        total_distance_km += route_distance
        orders_assigned += len(stops_out)
        routes_out.append(
            {
                "engineer_id": engineer.id,
                "engineer_name": engineer.name,
                "stops": stops_out,
                "total_distance_km": round(route_distance, 2),
            }
        )

    unassigned_out = [
        {"order_id": order_id, "reason": reason} for order_id, reason in state.unassigned_reasons.items()
    ]

    return {
        "routes": routes_out,
        "unassigned": unassigned_out,
        "metrics": {
            "engineers_used": engineers_used,
            "total_distance_km": round(total_distance_km, 2),
            "orders_assigned": orders_assigned,
            "orders_unassigned": len(unassigned_out),
        },
    }


def apply_event(
    event: ReplanEvent,
    orders: list[Order],
    engineers: list[Engineer],
    office: tuple[float, float],
    assignment: dict,
    builder: DistanceMatrixBuilder,
    blackouts: Blackouts | None = None,
) -> tuple[dict, dict]:
    """Применяет ОДНО событие перепланирования к уже посчитанному
    assignment (точечный патч, не пересчёт с нуля — см. модульный
    докстринг). Возвращает (plan, diff): plan — новый план той же формы,
    что assignment.json; diff — что именно изменилось (5 категорий, см.
    _build_diff). Матрицы расстояний строятся заново на весь набор точек
    (включая новую срочную заявку, если она есть) — если builder работает
    в другом режиме (force_fallback/наличие API-ключа), чем тот, что
    строил исходный assignment, числа даже у нетронутых маршрутов могут
    отличаться от исходных (единый источник правды важнее минимизации
    диффа, но конфигурацию builder стоит держать одинаковой между
    исходным прогоном и перепланированием). blackouts — окна недоступности
    из предыдущих событий сессии."""
    orders_by_id = {o.id: o for o in orders}
    engineers_by_id = {e.id: e for e in engineers}
    if event.type == "new_urgent_order":
        if event.new_order.id in orders_by_id:
            raise ValueError(
                f"Заявка с id {event.new_order.id!r} уже существует в orders.json — "
                f"событие new_urgent_order должно содержать новый, ещё не использованный id"
            )
        orders_by_id = {**orders_by_id, event.new_order.id: event.new_order}

    event_time_min = parse_hhmm(event.event_time)
    routes, original_reasons, unassigned_reasons, departure_after, frozen_counts = _load_current_state(
        assignment, orders_by_id, engineers_by_id, event_time_min
    )
    before_routes = {eid: list(stops) for eid, stops in routes.items()}
    original_routes_by_engineer = {route["engineer_id"]: route for route in assignment["routes"]}
    plan_blackouts = {eid: list(ws) for eid, ws in (blackouts or {}).items()}
    if event_time_min > 0:
        # Никто не выезжает к заявке раньше момента события: каждому
        # инженеру добавляем окно недоступности [0, event_time). Оно
        # касается только незамороженного хвоста.
        for engineer_id in engineers_by_id:
            plan_blackouts.setdefault(engineer_id, []).append((0, event_time_min))
    state = _PlanState(
        routes=routes, original_reasons=original_reasons, unassigned_reasons=unassigned_reasons,
        departure_after=departure_after, frozen_counts=frozen_counts,
        blackouts=plan_blackouts, event_time_min=event_time_min,
    )

    point_ids = ["__office__"] + list(orders_by_id.keys())
    point_index = {pid: i for i, pid in enumerate(point_ids)}
    points = [office] + [(o.lat, o.lon) for o in orders_by_id.values()]
    profiles_needed = {VEHICLE_TO_PROFILE[e.vehicle] for e in engineers}
    matrices = {profile: builder.build(profile, points) for profile in profiles_needed}

    cancelled: list[str] = []
    if event.type == "new_urgent_order":
        changes = _apply_new_urgent_order(event, engineers, engineers_by_id, orders_by_id, state, matrices, point_index)
    elif event.type == "cancel_order":
        cancelled = _apply_cancel_order(event, orders_by_id, engineers_by_id, state, matrices, point_index)
        changes = []
    elif event.type == "engineer_unavailable":
        changes = _apply_engineer_unavailable(
            event, engineers, engineers_by_id, orders_by_id, state, matrices, point_index
        )
    else:
        raise ValueError(f"Неизвестный тип события: {event.type!r}")

    plan = _build_replan_plan(
        state, orders_by_id, engineers_by_id, changes, matrices, point_index, original_routes_by_engineer
    )
    diff = _build_diff(changes, cancelled, before_routes, state.routes)

    return plan, diff


def _format_diff(diff: dict) -> str:
    lines = ["Изменения после перепланирования:"]
    for item in diff["newly_assigned"]:
        lines.append(f"  + {item['order_id']} -> {item['engineer_id']} (прибытие {item['arrival']})")
    for item in diff["reassigned"]:
        lines.append(
            f"  ~ {item['order_id']}: {item['from_engineer_id']} -> {item['to_engineer_id']} "
            f"(прибытие {item['arrival']})"
        )
    for item in diff["newly_unassigned"]:
        lines.append(f"  ? {item['order_id']} не назначена: {item['reason']}")
    for order_id in diff["cancelled"]:
        lines.append(f"  - {order_id} отменена")
    if len(lines) == 1:
        lines.append("  (без изменений)")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Применить одно событие перепланирования к текущему плану (ТЗ §2.1.6)."
    )
    parser.add_argument("--event-path", type=Path, required=True)
    parser.add_argument("--orders-path", type=Path, default=Path("data/output/orders.json"))
    parser.add_argument("--engineers-path", type=Path, default=Path("data/output/engineers.json"))
    parser.add_argument("--offices-path", type=Path, default=Path("data/output/offices.json"))
    parser.add_argument("--assignment-path", type=Path, default=Path("data/output/assignment.json"))
    parser.add_argument("--output", type=Path, default=Path("data/output/replan.json"))
    parser.add_argument(
        "--no-network", action="store_true", help="Не ходить в OpenRouteService, только haversine-фолбэк"
    )
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = _parse_args(argv)
    try:
        event = load_event(args.event_path)
        orders = load_orders(args.orders_path)
        engineers = load_engineers(args.engineers_path)
        office = load_office_coords(args.offices_path)
        assignment = json.loads(args.assignment_path.read_text(encoding="utf-8"))
        builder = DistanceMatrixBuilder(cache_path=args.cache_path, force_fallback=args.no_network)

        plan, diff = apply_event(event, orders, engineers, office, assignment, builder)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    event_out = {
        "type": event.type,
        "event_time": event.event_time,
        "order_id": event.order_id,
        "engineer_id": event.engineer_id,
        "from": event.unavailable_from,
        "until": event.unavailable_until,
        "new_order": asdict(event.new_order) if event.new_order is not None else None,
    }
    # routes/unassigned/metrics лежат на верхнем уровне, чтобы файл читался
    # build_map --assignment-path без изменений.
    output = {**plan, "event": event_out, "diff": diff}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_format_diff(diff))


if __name__ == "__main__":
    main()
