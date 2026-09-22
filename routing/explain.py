"""Человекочитаемые причины назначения/отказа для заявок (ТЗ §2.4.2)."""
from __future__ import annotations

from routing.models import Engineer, Order, parse_hhmm


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


def explain_unassigned(order: Order, engineers: list[Engineer]) -> str:
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

    # Навык, транспорт и время подходят, но заявку никому не поставили.
    # Точную причину (занятость, дорога, конкуренция за маршрут) отсюда
    # не определить — нет расписания и матрицы расстояний, поэтому
    # формулировка общая. Кандидатов может быть и один.
    candidate_word = "инженер" if len(overlapping_shift) == 1 else "инженеры"
    return (
        f"Формально подходящий по навыку, транспорту и времени {candidate_word} "
        f"есть, но заявку в итоге никому не поставили — точная причина "
        f"зависит от маршрута и расстояния до неё, а не только от навыка и времени"
    )


def explain_assigned(order: Order, engineer: Engineer, engineers: list[Engineer]) -> str:
    feasible = _feasible_engineers(order, engineers)
    if len(feasible) <= 1:
        return f"Единственный подходящий инженер по навыку «{order.required_skill}», транспорту и времени"
    return (
        f"Один из {len(feasible)} подходящих инженеров по навыку и времени — "
        f"выбран по критерию маршрута (минимум инженеров/пробега)"
    )
