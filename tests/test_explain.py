from routing.explain import explain_assigned, explain_unassigned
from routing.models import Engineer, Order

OFFICE = (55.70, 37.60)


def test_explain_unassigned_no_skill_anywhere():
    order = Order("1", 55.7, 37.6, 10, "09:00", "18:00", "Обычная", "Аварийные работы", None)
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]

    reason = explain_unassigned(order, engineers)

    assert "Аварийные работы" in reason
    assert "нет" in reason.lower()


def test_explain_unassigned_no_matching_transport():
    order = Order("1", 55.7, 37.6, 10, "09:00", "18:00", "Обычная", "Локальные работы", "Автомобиль")
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]

    reason = explain_unassigned(order, engineers)

    assert "Автомобиль" in reason


def test_explain_unassigned_no_time_overlap():
    order = Order("1", 55.7, 37.6, 10, "14:00", "16:00", "Обычная", "Локальные работы", None)
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "12:00", ["Локальные работы"], "Пешеход")]

    reason = explain_unassigned(order, engineers)

    assert "14:00" in reason and "16:00" in reason


def test_explain_unassigned_window_overlaps_but_duration_does_not_fit():
    # Окно 20:00-22:00 и смена 08:00-21:00 пересекаются как интервалы, но
    # работа на 70 минут физически не успевает ни при каком времени
    # прибытия (последний возможный старт — 19:50, до открытия окна).
    order = Order("1", 55.7, 37.6, 70, "20:00", "22:00", "Обычная", "Локальные работы", None)
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "21:00", ["Локальные работы"], "Пешеход")]

    reason = explain_unassigned(order, engineers)

    assert "70" in reason
    assert "успева" in reason.lower()


def test_explain_unassigned_does_not_invent_busy_when_engineer_is_idle():
    # Единственный свободный инженер не успевает доехать до узкого окна —
    # причина не должна называть его «занятым».
    order = Order("far", 55.9, 37.6, 10, "09:00", "09:10", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "Idle", *OFFICE, "09:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer])

    assert "заняты другими заявками" not in reason


def test_explain_assigned_unique_engineer():
    order = Order("1", 55.7, 37.6, 10, "09:00", "18:00", "Обычная", "Аварийные работы", None)
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Аварийные работы"], "Пешеход")
    other = Engineer("e2", "B", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")

    reason = explain_assigned(order, engineer, [engineer, other])

    assert "Единственный" in reason


def test_explain_assigned_multiple_candidates():
    order = Order("1", 55.7, 37.6, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    other = Engineer("e2", "B", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")

    reason = explain_assigned(order, engineer, [engineer, other])

    assert "2" in reason


def test_explain_unassigned_generic_fallback_does_not_claim_multiple_when_one():
    # Общее сообщение не должно говорить «несколько», если кандидат один.
    order = Order("far", 55.9, 37.6, 10, "09:00", "09:10", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "Idle", *OFFICE, "09:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer])

    assert "несколько" not in reason


def _travel(minutes):
    return lambda engineer: minutes


def test_explain_unassigned_says_cannot_reach_in_time():
    order = Order("far", 55.9, 37.6, 10, "09:00", "09:10", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "A", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer], _travel(120))

    assert "Не успевает доехать" in reason
    assert "120" in reason


def test_explain_unassigned_says_work_does_not_fit_shift_after_travel():
    order = Order("late", 55.9, 37.6, 60, "08:00", "18:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "A", *OFFICE, "08:00", "10:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer], _travel(90))

    assert "не укладывается в смену" in reason


def test_explain_unassigned_says_window_closed_before_event():
    order = Order("gone", 55.7, 37.6, 10, "09:00", "10:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "A", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer], _travel(5), not_before_min=11 * 60)

    assert "закрылось раньше момента события" in reason and "11:00" in reason


def test_explain_unassigned_reachable_engineer_means_busy():
    order = Order("ok", 55.7, 37.6, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "A", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer], _travel(10))

    assert "заняты другими заявками" in reason


def test_explain_unassigned_travel_estimate_uses_only_matching_engineers():
    # A приезжает к 09:25 (в окно), но не влезает в смену; B опаздывает по
    # окну. Минимум дороги для «не успевает доехать» берётся только по B.
    order = Order("x", 55.9, 37.6, 60, "09:00", "09:30", "Обычная", "Локальные работы", None)
    a = Engineer("a", "A", *OFFICE, "08:00", "10:00", ["Локальные работы"], "Пешеход")
    b = Engineer("b", "B", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Автомобиль")
    travel = lambda e: 85 if e.id == "a" else 200  # noqa: E731

    reason = explain_unassigned(order, [a, b], travel)

    assert "от 200 мин" in reason
    assert "85" not in reason


def test_explain_unassigned_window_closed_before_event_without_travel_data():
    order = Order("gone", 55.7, 37.6, 10, "09:00", "10:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e", "A", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")

    reason = explain_unassigned(order, [engineer], None, not_before_min=11 * 60)

    assert "закрылось раньше момента события" in reason
