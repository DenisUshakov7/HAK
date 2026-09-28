"""Человекочитаемые причины назначения/отказа для заявок (ТЗ §2.4.2)."""
from __future__ import annotations

import math

from routing.distance_matrix import VEHICLE_TO_PROFILE
from routing.models import Engineer, Order, format_hhmm, parse_hhmm


def _windows_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return parse_hhmm(a_start) < parse_hhmm(b_end) and parse_hhmm(b_start) < parse_hhmm(a_end)


def _fits_within_shift(order: Order, engineer: Engineer) -> bool:
    """Существует ли момент прибытия, при котором визит уложится и в окно
    заявки, и в смену инженера: arrival >= max(window_start, shift_start),
    arrival + duration_min <= min(window_end, shift_end). Зеркалит реальное
    ограничение времени в routing/solve.py (AddDimensionWithVehicleTransits +
    CumulVar) — просто пересечения интервалов окна и смены недостаточно:
    они могут пересекаться, а длительность работы всё равно не влезать
    (например, окно 20:00-22:00, смена до 21:00, работа на 70 минут)."""
    window_start = parse_hhmm(order.window_start)
    window_end = parse_hhmm(order.window_end)
    shift_start = parse_hhmm(engineer.shift_start)
    shift_end = parse_hhmm(engineer.shift_end)
    earliest_arrival = max(window_start, shift_start)
    latest_arrival = min(window_end, shift_end - order.duration_min)
    return earliest_arrival <= latest_arrival


def _with_skill_and_transport(order: Order, engineers: list[Engineer]) -> list[Engineer]:
    candidates = [e for e in engineers if order.required_skill in e.skills]
    if order.required_transport is not None:
        candidates = [e for e in candidates if e.vehicle == order.required_transport]
    return candidates


def _feasible_engineers(order: Order, engineers: list[Engineer]) -> list[Engineer]:
    return [e for e in _with_skill_and_transport(order, engineers) if _fits_within_shift(order, e)]


def explain_unassigned(
    order: Order,
    engineers: list[Engineer],
    travel_min=None,
    not_before_min: int = 0,
) -> str:
    """Понятная диспетчеру причина, почему заявка не назначена. travel_min —
    необязательная функция engineer -> минуты дороги из офиса до заявки
    (см. office_travel_fn); not_before_min — момент события в минутах: раньше
    него никто выехать не может (нужно при перепланировании)."""
    with_skill = [e for e in engineers if order.required_skill in e.skills]
    if not with_skill:
        return f"Нет ни одного инженера с навыком «{order.required_skill}»"

    if order.required_transport is not None:
        with_transport = [e for e in with_skill if e.vehicle == order.required_transport]
        if not with_transport:
            return (
                f"Нет инженера с навыком «{order.required_skill}» "
                f"и транспортом «{order.required_transport}»"
            )

    candidates = _with_skill_and_transport(order, engineers)
    overlapping_shift = [
        e
        for e in candidates
        if _windows_overlap(order.window_start, order.window_end, e.shift_start, e.shift_end)
    ]
    if not overlapping_shift:
        return f"Ни один подходящий инженер не работает в окно {order.window_start}–{order.window_end}"

    if not any(_fits_within_shift(order, e) for e in overlapping_shift):
        return (
            f"Работа занимает {order.duration_min} мин — ни один подходящий инженер "
            f"не успевает выполнить её до конца смены в пределах окна "
            f"{order.window_start}–{order.window_end}"
        )

    fitting = [e for e in overlapping_shift if _fits_within_shift(order, e)]

    if not_before_min > parse_hhmm(order.window_end):
        return (
            f"Окно заявки {order.window_start}–{order.window_end} закрылось раньше "
            f"момента события ({format_hhmm(not_before_min)})"
        )

    if travel_min is not None:
        reason = _explain_by_travel(order, fitting, travel_min)
        if reason is not None:
            return reason
        return (
            "Подходящие по навыку и времени инженеры есть, но встроить заявку "
            "в их маршруты не удалось: в это время они заняты другими заявками"
        )

    # Без данных о дороге (или при перепланировании, где инженеры уже не в
    # офисе) точную причину не определить — формулировка общая.
    who = "подходит один инженер" if len(fitting) == 1 else "подходят инженеры"
    return (
        f"Формально {who} (по навыку, транспорту и времени), но заявку не удалось "
        f"встроить в маршруты с учётом текущего плана"
    )


def _explain_by_travel(order: Order, engineers: list[Engineer], travel_min) -> str | None:
    """Конкретная причина по дороге из офиса (только для первичного плана,
    где все инженеры стартуют из офиса в начале смены). None — если хотя бы
    один инженер в одиночку успел бы (тогда заявка не встроилась из-за
    других заявок в маршрутах)."""
    window_start = parse_hhmm(order.window_start)
    window_end = parse_hhmm(order.window_end)

    late_travels: list[float] = []
    no_room_travels: list[float] = []
    for engineer in engineers:
        travel = travel_min(engineer)
        if not math.isfinite(travel):
            continue
        start = max(parse_hhmm(engineer.shift_start) + round(travel), window_start)
        if start > window_end:
            late_travels.append(travel)
        elif start + order.duration_min > parse_hhmm(engineer.shift_end):
            no_room_travels.append(travel)
        else:
            return None

    if late_travels:
        return (
            f"Не успевает доехать: подходящему инженеру ехать от {round(min(late_travels))} мин, "
            f"а окно {order.window_start}–{order.window_end} закрывается раньше"
        )
    if no_room_travels:
        return (
            f"После дороги (от {round(min(no_room_travels))} мин) работа на "
            f"{order.duration_min} мин не укладывается в смену подходящих инженеров"
        )
    return "До заявки нет пути для транспорта подходящих инженеров"


def office_travel_fn(matrices: dict, office_index: int, order_index: int):
    """travel_min для explain_unassigned: минуты дороги из офиса до заявки
    по матрице профиля транспорта инженера (inf, если профиля нет)."""

    def travel(engineer: Engineer) -> float:
        matrix = matrices.get(VEHICLE_TO_PROFILE[engineer.vehicle])
        if matrix is None:
            return math.inf
        return matrix.duration_min[(office_index, order_index)]

    return travel


def explain_assigned(order: Order, engineer: Engineer, engineers: list[Engineer]) -> str:
    feasible = _feasible_engineers(order, engineers)
    if len(feasible) <= 1:
        return f"Единственный подходящий инженер по навыку «{order.required_skill}», транспорту и времени"
    return (
        f"Один из {len(feasible)} подходящих инженеров по навыку и времени — "
        f"выбран по критерию маршрута (минимум инженеров/пробега)"
    )
