import json

import pytest

from routing.distance_matrix import DistanceMatrixBuilder, MatrixResult
from routing.models import Engineer, Order, parse_hhmm
from routing.replan import ReplanEvent, StopTiming, _find_cheapest_insertion, _simulate_tail, apply_event, load_event


def test_load_event_new_urgent_order(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "type": "new_urgent_order",
                "event_time": "11:00",
                "id": "999",
                "lat": 55.71,
                "lon": 37.62,
                "duration_min": 20,
                "window_start": "11:00",
                "window_end": "13:00",
                "priority": "Срочная",
                "required_skill": "Аварийные работы",
                "required_transport": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    event = load_event(path)

    assert event.type == "new_urgent_order"
    assert event.event_time == "11:00"
    assert event.new_order == Order(
        "999", 55.71, 37.62, 20, "11:00", "13:00", "Срочная", "Аварийные работы", None
    )
    assert event.order_id is None
    assert event.engineer_id is None


def test_load_event_cancel_order(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps({"type": "cancel_order", "event_time": "11:00", "order_id": "32807"}, ensure_ascii=False),
        encoding="utf-8",
    )

    event = load_event(path)

    assert event.type == "cancel_order"
    assert event.event_time == "11:00"
    assert event.order_id == "32807"
    assert event.new_order is None
    assert event.engineer_id is None


def test_load_event_engineer_unavailable(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps({"type": "engineer_unavailable", "event_time": "11:00", "engineer_id": "eng_03"}, ensure_ascii=False),
        encoding="utf-8",
    )

    event = load_event(path)

    assert event.type == "engineer_unavailable"
    assert event.engineer_id == "eng_03"
    assert event.order_id is None
    assert event.new_order is None


def test_load_event_unknown_type_raises(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps({"type": "something_else", "event_time": "11:00"}, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="something_else"):
        load_event(path)


OFFICE = (55.70, 37.60)


def _matrix(pairs: dict[tuple[int, int], tuple[float, float]]) -> MatrixResult:
    distance_km = {key: value[0] for key, value in pairs.items()}
    duration_min = {key: value[1] for key, value in pairs.items()}
    return MatrixResult(distance_km=distance_km, duration_min=duration_min, source="test")


def test_simulate_tail_succeeds_and_returns_timings():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order}
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}

    result = _simulate_tail(engineer, "__office__", parse_hhmm("08:00"), ["A"], orders_by_id, matrix, point_index)

    assert result == [StopTiming("A", parse_hhmm("09:00"), 10, 1.0)]


def test_simulate_tail_chains_multiple_stops():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_a = Order("A", 55.701, 37.601, 10, "09:00", "20:00", "Обычная", "Локальные работы", None)
    order_b = Order("B", 55.702, 37.602, 10, "09:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order_a, "B": order_b}
    matrix = _matrix({(0, 1): (1.0, 10.0), (1, 2): (2.0, 15.0)})
    point_index = {"__office__": 0, "A": 1, "B": 2}

    result = _simulate_tail(
        engineer, "__office__", parse_hhmm("08:00"), ["A", "B"], orders_by_id, matrix, point_index
    )

    assert result[0] == StopTiming("A", parse_hhmm("09:00"), 10, 1.0)
    assert result[1] == StopTiming("B", parse_hhmm("09:00") + 10 + 15, 15, 2.0)


def test_simulate_tail_rejects_when_start_exceeds_window_end():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 10, "07:00", "07:05", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order}
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}

    result = _simulate_tail(engineer, "__office__", parse_hhmm("08:00"), ["A"], orders_by_id, matrix, point_index)

    assert result is None


def test_simulate_tail_rejects_when_completion_exceeds_shift_end():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 700, "09:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order}
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}

    result = _simulate_tail(engineer, "__office__", parse_hhmm("08:00"), ["A"], orders_by_id, matrix, point_index)

    assert result is None


def test_find_cheapest_insertion_picks_lowest_cost_position():
    # N физически лежит на прямой между A и B — вставка между ними ничего
    # не стоит (cost=0), это должно победить и позицию перед A (cost=2), и
    # позицию после B (cost=4).
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_a = Order("A", 55.70, 37.605, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    order_b = Order("B", 55.70, 37.610, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    new_order = Order("N", 55.70, 37.606, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order_a, "B": order_b, "N": new_order}
    point_index = {"__office__": 0, "A": 1, "B": 2, "N": 3}
    matrix = _matrix(
        {
            (0, 1): (5.0, 5.0), (1, 0): (5.0, 5.0),
            (0, 2): (10.0, 10.0), (2, 0): (10.0, 10.0),
            (0, 3): (6.0, 6.0), (3, 0): (6.0, 6.0),
            (1, 2): (5.0, 5.0), (2, 1): (5.0, 5.0),
            (1, 3): (1.0, 1.0), (3, 1): (1.0, 1.0),
            (2, 3): (4.0, 4.0), (3, 2): (4.0, 4.0),
        }
    )
    matrices = {"foot": matrix}
    routes = {"e1": ["A", "B"]}
    departure_after = {"e1": [480, 495, 510]}
    frozen_counts = {"e1": 0}

    result = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, frozen_counts, orders_by_id, matrices, point_index
    )

    assert result == (0.0, "e1", 1)


def test_find_cheapest_insertion_excludes_frozen_positions():
    # Без заморозки позиция 0 дешевле всего (N рядом с офисом). С
    # frozen_counts["e1"]=1 позиция 0 исключена, несмотря на то что
    # дешевле всего — должна победить позиция 1.
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_a = Order("A", 55.70, 37.605, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    order_b = Order("B", 55.70, 37.610, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    new_order = Order("N", 55.70, 37.601, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order_a, "B": order_b, "N": new_order}
    point_index = {"__office__": 0, "A": 1, "B": 2, "N": 3}
    matrix = _matrix(
        {
            (0, 1): (5.0, 5.0), (1, 0): (5.0, 5.0),
            (0, 2): (10.0, 10.0), (2, 0): (10.0, 10.0),
            (0, 3): (1.0, 1.0), (3, 0): (1.0, 1.0),
            (1, 2): (5.0, 5.0), (2, 1): (5.0, 5.0),
            (1, 3): (4.0, 4.0), (3, 1): (4.0, 4.0),
            (2, 3): (9.0, 9.0), (3, 2): (9.0, 9.0),
        }
    )
    matrices = {"foot": matrix}
    routes = {"e1": ["A", "B"]}
    departure_after = {"e1": [480, 495, 510]}

    result_open = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, {"e1": 0}, orders_by_id, matrices, point_index
    )
    assert result_open == (0.0, "e1", 0)

    result_frozen = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, {"e1": 1}, orders_by_id, matrices, point_index
    )
    assert result_frozen == (8.0, "e1", 1)


def test_find_cheapest_insertion_returns_none_when_no_skill_match():
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    new_order = Order("N", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Аварийные работы", None)
    orders_by_id = {"N": new_order}
    point_index = {"__office__": 0, "N": 1}
    matrices = {"foot": _matrix({(0, 1): (1.0, 10.0)})}
    routes = {"e1": []}
    departure_after = {"e1": [480]}
    frozen_counts = {"e1": 0}

    result = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, frozen_counts, orders_by_id, matrices, point_index
    )

    assert result is None


def test_find_cheapest_insertion_appends_at_end_when_cheapest():
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_a = Order("A", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    new_order = Order("N", 55.702, 37.602, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order_a, "N": new_order}
    point_index = {"__office__": 0, "A": 1, "N": 2}
    matrix = _matrix(
        {
            (0, 1): (5.0, 5.0), (1, 0): (5.0, 5.0),
            (0, 2): (100.0, 100.0), (2, 0): (100.0, 100.0),
            (1, 2): (2.0, 2.0), (2, 1): (2.0, 2.0),
        }
    )
    matrices = {"foot": matrix}
    routes = {"e1": ["A"]}
    departure_after = {"e1": [480, 495]}
    frozen_counts = {"e1": 0}

    result = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, frozen_counts, orders_by_id, matrices, point_index
    )

    assert result == (2.0, "e1", 1)


def test_find_cheapest_insertion_compares_across_engineers():
    e1 = Engineer("e1", "Busy", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    e2 = Engineer("e2", "Idle", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_a = Order("A", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    new_order = Order("N", 55.7011, 37.6011, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"A": order_a, "N": new_order}
    point_index = {"__office__": 0, "A": 1, "N": 2}
    matrix = _matrix(
        {
            (0, 1): (10.0, 10.0), (1, 0): (10.0, 10.0),
            (0, 2): (50.0, 50.0), (2, 0): (50.0, 50.0),
            (1, 2): (1.0, 1.0), (2, 1): (1.0, 1.0),
        }
    )
    matrices = {"foot": matrix}
    routes = {"e1": ["A"], "e2": []}
    departure_after = {"e1": [480, 500], "e2": [480]}
    frozen_counts = {"e1": 0, "e2": 0}

    result = _find_cheapest_insertion(
        new_order, [e1, e2], routes, departure_after, frozen_counts, orders_by_id, matrices, point_index
    )

    assert result == (1.0, "e1", 1)


def test_find_cheapest_insertion_rejects_cheapest_when_it_breaks_downstream_window():
    # Вставка N перед B дешевле всего по формуле стоимости (cost=-8), но
    # сдвигает время начала работы B за пределы его узкого окна
    # (492 > 491) — должна быть отклонена пересимуляцией хвоста, и
    # победить должна более дорогая, но допустимая вставка в конец
    # маршрута (cost=1). Без реальной пересимуляции хвоста алгоритм
    # ошибочно вернул бы более дешёвую, но недопустимую позицию 0.
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order_b = Order("B", 55.701, 37.601, 10, "08:00", "08:11", "Обычная", "Локальные работы", None)
    new_order = Order("N", 55.702, 37.602, 10, "08:00", "20:00", "Обычная", "Локальные работы", None)
    orders_by_id = {"B": order_b, "N": new_order}
    point_index = {"__office__": 0, "B": 1, "N": 2}
    matrix = _matrix(
        {
            (0, 1): (10.0, 10.0), (1, 0): (10.0, 10.0),
            (0, 2): (1.0, 1.0), (2, 0): (1.0, 1.0),
            (1, 2): (1.0, 1.0), (2, 1): (1.0, 1.0),
        }
    )
    matrices = {"foot": matrix}
    routes = {"e1": ["B"]}
    departure_after = {"e1": [480, 500]}
    frozen_counts = {"e1": 0}

    result = _find_cheapest_insertion(
        new_order, [engineer], routes, departure_after, frozen_counts, orders_by_id, matrices, point_index
    )

    assert result == (1.0, "e1", 1)


def _assignment(routes, unassigned=None):
    return {"routes": routes, "unassigned": unassigned or [], "metrics": {}}


def _route(engineer_id, engineer_name, stops, total_distance_km=0.0):
    return {
        "engineer_id": engineer_id,
        "engineer_name": engineer_name,
        "stops": stops,
        "total_distance_km": total_distance_km,
    }


def _stop(order_id, arrival, travel_min=10, distance_km=1.0, reason="Исходная причина"):
    return {
        "order_id": order_id,
        "arrival": arrival,
        "travel_min": travel_min,
        "distance_km": distance_km,
        "reason": reason,
    }


def _builder(tmp_path):
    return DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", force_fallback=True)


def test_apply_new_urgent_order_assigns_to_some_engineer(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(
        type="new_urgent_order",
        event_time="08:30",
        new_order=Order("999", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert len(diff["newly_assigned"]) == 1
    assert diff["newly_assigned"][0]["order_id"] == "999"
    assigned_order_ids = {s["order_id"] for r in plan["routes"] for s in r["stops"]}
    assert "999" in assigned_order_ids
    assert plan["metrics"]["orders_assigned"] == 2


def test_apply_new_urgent_order_unassigned_when_no_skill_match(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(
        type="new_urgent_order",
        event_time="08:30",
        new_order=Order("999", 55.702, 37.602, 10, "09:00", "18:00", "Срочная", "Секретный навык", None),
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["newly_assigned"] == []
    assert len(diff["newly_unassigned"]) == 1
    assert diff["newly_unassigned"][0]["order_id"] == "999"
    assert {u["order_id"] for u in plan["unassigned"]} == {"999"}


def test_apply_cancel_order_removes_stop_and_recomputes_tail(tmp_path):
    orders = [
        Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment(
        [_route("e1", "Тест", [_stop("1", "09:10"), _stop("2", "09:30")])]
    )
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["cancelled"] == ["1"]
    assert diff["newly_unassigned"] == []
    remaining_ids = {s["order_id"] for r in plan["routes"] for s in r["stops"]}
    assert remaining_ids == {"2"}
    assert all(u["order_id"] != "1" for u in plan["unassigned"])


def test_apply_cancel_order_frozen_raises(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(type="cancel_order", event_time="10:00", order_id="1")  # позже arrival 09:10

    with pytest.raises(ValueError, match="1"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_apply_cancel_order_nonexistent_raises(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="does_not_exist")

    with pytest.raises(ValueError, match="does_not_exist"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_apply_cancel_order_already_unassigned_just_removed(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([], unassigned=[{"order_id": "1", "reason": "Была не назначена"}])
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["cancelled"] == ["1"]
    assert plan["unassigned"] == []


def test_apply_engineer_unavailable_reassigns_open_orders(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [
        Engineer("e1", "Недоступный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment([_route("e1", "Недоступный", [_stop("1", "09:10")])])
    event = ReplanEvent(type="engineer_unavailable", event_time="08:30", engineer_id="e1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert len(diff["reassigned"]) == 1
    assert diff["reassigned"][0] == {
        "order_id": "1",
        "from_engineer_id": "e1",
        "to_engineer_id": "e2",
        "arrival": diff["reassigned"][0]["arrival"],
    }
    e1_route = next((r for r in plan["routes"] if r["engineer_id"] == "e1"), None)
    assert e1_route is None  # у e1 больше нет остановок вообще
    e2_route = next(r for r in plan["routes"] if r["engineer_id"] == "e2")
    assert [s["order_id"] for s in e2_route["stops"]] == ["1"]


def test_apply_engineer_unavailable_frozen_orders_stay(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [
        Engineer("e1", "Недоступный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment([_route("e1", "Недоступный", [_stop("1", "09:10")])])
    event = ReplanEvent(type="engineer_unavailable", event_time="10:00", engineer_id="e1")  # позже arrival 09:10

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["reassigned"] == []
    e1_route = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert [s["order_id"] for s in e1_route["stops"]] == ["1"]


def test_apply_engineer_unavailable_nonexistent_raises(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(type="engineer_unavailable", event_time="08:30", engineer_id="does_not_exist")

    with pytest.raises(ValueError, match="does_not_exist"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_apply_engineer_unavailable_prioritizes_urgent_orders(tmp_path):
    # У недоступного инженера — две открытые заявки, "Обычная" идёт в
    # маршруте первой, "Срочная" второй. Второй, полностью свободный
    # инженер без проблем вмещает обе — тест не про нехватку места, а
    # про порядок ОБРАБОТКИ: несмотря на то что "Обычная" была в
    # исходном маршруте раньше, "Срочная" должна попасть в diff первой
    # (ТЗ §2.4.1 — приоритет при перепланировании, не про исход вставки).
    orders = [
        Order("normal", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("urgent", 55.702, 37.602, 10, "09:00", "18:00", "Срочная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Недоступный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment(
        [_route("e1", "Недоступный", [_stop("normal", "09:10"), _stop("urgent", "09:30")])]
    )
    event = ReplanEvent(type="engineer_unavailable", event_time="08:30", engineer_id="e1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    urgent_change = next(c for c in diff["reassigned"] + diff["newly_unassigned"] if c["order_id"] == "urgent")
    normal_change = next(c for c in diff["reassigned"] + diff["newly_unassigned"] if c["order_id"] == "normal")
    order_processed = [c["order_id"] for c in diff["reassigned"] + diff["newly_unassigned"]]
    assert order_processed.index("urgent") < order_processed.index("normal")


def test_apply_event_unknown_type_raises(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(type="something_else", event_time="08:30")

    with pytest.raises(ValueError, match="something_else"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_plan_shape_matches_assignment_format(tmp_path):
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert set(plan.keys()) == {"routes", "unassigned", "metrics"}
    assert set(plan["metrics"].keys()) == {
        "engineers_used", "total_distance_km", "orders_assigned", "orders_unassigned",
    }
    assert set(diff.keys()) == {
        "newly_assigned", "newly_unassigned", "reassigned", "cancelled", "routes_changed",
    }


def test_apply_event_raises_clear_error_for_stale_engineer_in_assignment(tmp_path):
    # assignment.json ссылается на инженера, которого больше нет в
    # engineers.json — тот же класс устаревания, что routemap/build_map.py
    # уже обрабатывает через _engineer_or_raise, а не сырой KeyError.
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers: list[Engineer] = []
    assignment = _assignment([_route("ghost_engineer", "Призрак", [_stop("1", "09:10")])])
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="1")

    with pytest.raises(ValueError, match="ghost_engineer"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_apply_event_raises_clear_error_for_stale_order_in_assignment(tmp_path):
    # assignment.json ссылается на заявку, которой больше нет в orders.json.
    orders: list[Order] = []
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("ghost_order", "09:10")])])
    event = ReplanEvent(type="engineer_unavailable", event_time="08:30", engineer_id="e1")

    with pytest.raises(ValueError, match="ghost_order"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_apply_new_urgent_order_raises_for_duplicate_id(tmp_path):
    # Событие new_urgent_order с id, который уже есть в orders.json,
    # раньше молча затирало существующую заявку вместо явной ошибки.
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Тест", [_stop("1", "09:10")])])
    event = ReplanEvent(
        type="new_urgent_order",
        event_time="08:30",
        new_order=Order("1", 55.702, 37.602, 10, "09:00", "18:00", "Срочная", "Локальные работы", None),
    )

    with pytest.raises(ValueError, match="уже существует"):
        apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))


def test_build_diff_routes_changed_only_includes_touched_engineer(tmp_path):
    orders = [
        Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Первый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Второй", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment(
        [
            _route("e1", "Первый", [_stop("1", "09:10")]),
            _route("e2", "Второй", [_stop("2", "09:10")]),
        ]
    )
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert [r["engineer_id"] for r in diff["routes_changed"]] == ["e1"]
    assert diff["routes_changed"][0]["before_stops"] == ["1"]
    assert diff["routes_changed"][0]["after_stops"] == []


from pathlib import Path

from routing.replan import _format_diff, _parse_args, main


OFFICE_RECORD = {"source_file": "x.csv", "address": "тест", "lat": 55.70, "lon": 37.60, "geocode_source": "nominatim"}


def _order_record(id_, lat, lon, skill="Локальные работы"):
    return {
        "id": id_, "address": "тест", "district": "тест", "lat": lat, "lon": lon,
        "geocode_source": "nominatim", "duration_min": 10, "window_start": "09:00",
        "window_end": "18:00", "date": "2026-08-17", "priority": "Обычная",
        "required_skill": skill, "required_transport": None, "gigabit": False, "source_file": "x.csv",
    }


def _engineer_record(id_, name, skills=None, vehicle="Пешеход"):
    return {
        "id": id_, "name": name, "start_lat": OFFICE_RECORD["lat"], "start_lon": OFFICE_RECORD["lon"],
        "shift_start": "08:00", "shift_end": "20:00", "skills": skills or ["Локальные работы"], "vehicle": vehicle,
    }


def _write_replan_cli_inputs(tmp_path):
    orders_path = tmp_path / "orders.json"
    engineers_path = tmp_path / "engineers.json"
    offices_path = tmp_path / "offices.json"
    assignment_path = tmp_path / "assignment.json"

    orders_path.write_text(
        json.dumps([_order_record("1", 55.701, 37.601)], ensure_ascii=False), encoding="utf-8"
    )
    engineers_path.write_text(
        json.dumps([_engineer_record("e1", "Тест")], ensure_ascii=False), encoding="utf-8"
    )
    offices_path.write_text(json.dumps([OFFICE_RECORD], ensure_ascii=False), encoding="utf-8")
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [_route("e1", "Тест", [_stop("1", "09:10")])],
                "unassigned": [],
                "metrics": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return orders_path, engineers_path, offices_path, assignment_path


def _write_event(tmp_path, event_dict):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event_dict, ensure_ascii=False), encoding="utf-8")
    return path


def test_parse_args_defaults():
    args = _parse_args(["--event-path", "event.json"])
    assert args.event_path == Path("event.json")
    assert args.orders_path == Path("data/output/orders.json")
    assert args.engineers_path == Path("data/output/engineers.json")
    assert args.offices_path == Path("data/output/offices.json")
    assert args.assignment_path == Path("data/output/assignment.json")
    assert args.output == Path("data/output/replan.json")
    assert args.no_network is False


def test_main_writes_replan_json_and_prints_diff(tmp_path, capsys):
    orders_path, engineers_path, offices_path, assignment_path = _write_replan_cli_inputs(tmp_path)
    event_path = _write_event(tmp_path, {"type": "cancel_order", "event_time": "08:30", "order_id": "1"})
    output_path = tmp_path / "replan.json"

    main(
        [
            "--event-path", str(event_path),
            "--orders-path", str(orders_path),
            "--engineers-path", str(engineers_path),
            "--offices-path", str(offices_path),
            "--assignment-path", str(assignment_path),
            "--output", str(output_path),
            "--no-network",
        ]
    )

    assert output_path.exists()
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["diff"]["cancelled"] == ["1"]
    assert "routes" in written
    assert "unassigned" in written
    assert "metrics" in written
    assert "event" in written
    captured = capsys.readouterr()
    assert "1" in captured.out


def test_main_prints_clean_error_and_exits_nonzero_for_missing_event(tmp_path, capsys):
    orders_path, engineers_path, offices_path, assignment_path = _write_replan_cli_inputs(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--event-path", str(tmp_path / "does_not_exist.json"),
                "--orders-path", str(orders_path),
                "--engineers-path", str(engineers_path),
                "--offices-path", str(offices_path),
                "--assignment-path", str(assignment_path),
                "--output", str(tmp_path / "replan.json"),
                "--no-network",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "Traceback" not in captured.err


def test_main_prints_clean_error_for_cancel_of_nonexistent_order(tmp_path, capsys):
    orders_path, engineers_path, offices_path, assignment_path = _write_replan_cli_inputs(tmp_path)
    event_path = _write_event(tmp_path, {"type": "cancel_order", "event_time": "08:30", "order_id": "ghost"})

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--event-path", str(event_path),
                "--orders-path", str(orders_path),
                "--engineers-path", str(engineers_path),
                "--offices-path", str(offices_path),
                "--assignment-path", str(assignment_path),
                "--output", str(tmp_path / "replan.json"),
                "--no-network",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "ghost" in captured.err


def test_main_replan_json_preserves_new_order_data(tmp_path):
    # В replan.json должны сохраняться данные самой срочной заявки.
    orders_path, engineers_path, offices_path, assignment_path = _write_replan_cli_inputs(tmp_path)
    event_path = _write_event(
        tmp_path,
        {
            "type": "new_urgent_order",
            "event_time": "08:30",
            "id": "999",
            "lat": 55.702,
            "lon": 37.602,
            "duration_min": 10,
            "window_start": "09:00",
            "window_end": "18:00",
            "priority": "Срочная",
            "required_skill": "Локальные работы",
            "required_transport": None,
        },
    )
    output_path = tmp_path / "replan.json"

    main(
        [
            "--event-path", str(event_path),
            "--orders-path", str(orders_path),
            "--engineers-path", str(engineers_path),
            "--offices-path", str(offices_path),
            "--assignment-path", str(assignment_path),
            "--output", str(output_path),
            "--no-network",
        ]
    )

    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["event"]["new_order"]["id"] == "999"
    assert written["event"]["new_order"]["lat"] == 55.702
    assert written["event"]["new_order"]["required_skill"] == "Локальные работы"


def test_format_diff_output_is_windows_console_safe():
    # Вывод должен кодироваться в cp1251 (консоль Windows): без символов
    # вроде "→" (иначе print падает с UnicodeEncodeError).
    diff = {
        "newly_assigned": [{"order_id": "1", "engineer_id": "e1", "arrival": "10:00"}],
        "newly_unassigned": [{"order_id": "2", "reason": "причина"}],
        "reassigned": [{"order_id": "3", "from_engineer_id": "e1", "to_engineer_id": "e2", "arrival": "11:00"}],
        "cancelled": ["4"],
        "routes_changed": [],
    }

    text = _format_diff(diff)

    text.encode("cp1251")


def test_main_replan_json_is_directly_consumable_by_build_map(tmp_path):
    # build_map ожидает routes/unassigned/metrics на верхнем уровне файла,
    # поэтому карту можно строить прямо по replan.json.
    from routemap.build_map import run as build_map_run

    orders_path, engineers_path, offices_path, assignment_path = _write_replan_cli_inputs(tmp_path)
    event_path = _write_event(tmp_path, {"type": "cancel_order", "event_time": "08:30", "order_id": "1"})
    replan_path = tmp_path / "replan.json"

    main(
        [
            "--event-path", str(event_path),
            "--orders-path", str(orders_path),
            "--engineers-path", str(engineers_path),
            "--offices-path", str(offices_path),
            "--assignment-path", str(assignment_path),
            "--output", str(replan_path),
            "--no-network",
        ]
    )

    map_output = build_map_run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=replan_path,
        output_path=tmp_path / "replan_map.html",
        comparison_path=tmp_path / "no_comparison.json",
    )

    assert map_output.exists()


def test_apply_event_reuses_untouched_route_without_resimulating_against_different_matrix(tmp_path):
    # Маршрут, которого событие не касалось, берётся из плана как есть, даже
    # если текущая матрица (например, haversine без сети) с ним не согласуется:
    # записанные 08:05 при пересчёте (office -> far ~22 км пешком) не влезли бы
    # в окно 08:00-08:10.
    orders = [
        Order("far", 55.90, 37.60, 10, "08:00", "08:10", "Обычная", "Локальные работы", None),
        Order("cancel_me", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Нетронутый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Затронутый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment(
        [
            _route("e1", "Нетронутый", [_stop("far", "08:05", travel_min=5, distance_km=1.0)]),
            _route("e2", "Затронутый", [_stop("cancel_me", "09:10")]),
        ]
    )
    event = ReplanEvent(type="cancel_order", event_time="08:30", order_id="cancel_me")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    e1_route = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert e1_route["stops"][0]["arrival"] == "08:05"  # исходные данные переиспользованы как есть, без пересимуляции


from routing.replan import _push_out_of_blackouts


def test_push_out_of_blackouts_moves_time_to_window_end():
    windows = [(parse_hhmm("09:00"), parse_hhmm("10:00"))]
    assert _push_out_of_blackouts(parse_hhmm("09:30"), windows) == parse_hhmm("10:00")
    assert _push_out_of_blackouts(parse_hhmm("10:00"), windows) == parse_hhmm("10:00")
    assert _push_out_of_blackouts(parse_hhmm("08:59"), windows) == parse_hhmm("08:59")


def test_push_out_of_blackouts_handles_adjacent_windows():
    windows = [(parse_hhmm("09:00"), parse_hhmm("10:00")), (parse_hhmm("10:00"), parse_hhmm("11:00"))]
    assert _push_out_of_blackouts(parse_hhmm("09:30"), windows) == parse_hhmm("11:00")


def test_simulate_tail_shifts_start_out_of_blackout():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}
    blackouts = [(parse_hhmm("09:00"), parse_hhmm("10:00"))]

    result = _simulate_tail(
        engineer, "__office__", parse_hhmm("08:00"), ["A"], {"A": order}, matrix, point_index, blackouts
    )

    assert result == [StopTiming("A", parse_hhmm("10:00"), 10, 1.0)]


def test_simulate_tail_rejects_when_blackout_shift_breaks_order_window():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 10, "09:00", "09:30", "Обычная", "Локальные работы", None)
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}
    blackouts = [(parse_hhmm("09:00"), parse_hhmm("10:00"))]

    result = _simulate_tail(
        engineer, "__office__", parse_hhmm("08:00"), ["A"], {"A": order}, matrix, point_index, blackouts
    )

    assert result is None


def test_simulate_tail_ignores_blackout_that_does_not_contain_start():
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    order = Order("A", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)
    matrix = _matrix({(0, 1): (1.0, 10.0)})
    point_index = {"__office__": 0, "A": 1}
    blackouts = [(parse_hhmm("11:00"), parse_hhmm("12:00"))]

    result = _simulate_tail(
        engineer, "__office__", parse_hhmm("08:00"), ["A"], {"A": order}, matrix, point_index, blackouts
    )

    assert result == [StopTiming("A", parse_hhmm("09:00"), 10, 1.0)]


def test_find_cheapest_insertion_respects_blackouts():
    engineer = Engineer("e1", "A-eng", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")
    new_order = Order("N", 55.701, 37.601, 10, "09:00", "09:30", "Обычная", "Локальные работы", None)
    orders_by_id = {"N": new_order}
    point_index = {"__office__": 0, "N": 1}
    matrices = {"foot": _matrix({(0, 1): (1.0, 10.0)})}
    args = (new_order, [engineer], {"e1": []}, {"e1": [480]}, {"e1": 0}, orders_by_id, matrices, point_index)

    blocked = _find_cheapest_insertion(*args, blackouts={"e1": [(parse_hhmm("09:00"), parse_hhmm("10:00"))]})
    free = _find_cheapest_insertion(*args, blackouts={"e1": [(parse_hhmm("11:00"), parse_hhmm("12:00"))]})
    default = _find_cheapest_insertion(*args)

    assert blocked is None
    assert free == (1.0, "e1", 0)
    assert default == (1.0, "e1", 0)


from routing.replan import blackout_for_event


def test_blackout_for_event_defaults_to_event_time_and_end_of_day():
    event = ReplanEvent(type="engineer_unavailable", event_time="11:00", engineer_id="e1")
    assert blackout_for_event(event) == ("e1", parse_hhmm("11:00"), 1440)


def test_blackout_for_event_uses_from_and_until_and_clamps_from_to_event_time():
    event = ReplanEvent(
        type="engineer_unavailable", event_time="11:00", engineer_id="e1",
        unavailable_from="10:00", unavailable_until="13:00",
    )
    assert blackout_for_event(event) == ("e1", parse_hhmm("11:00"), parse_hhmm("13:00"))
    later = ReplanEvent(
        type="engineer_unavailable", event_time="11:00", engineer_id="e1",
        unavailable_from="12:00", unavailable_until="13:00",
    )
    assert blackout_for_event(later) == ("e1", parse_hhmm("12:00"), parse_hhmm("13:00"))


def test_blackout_for_event_rejects_until_not_after_from():
    event = ReplanEvent(
        type="engineer_unavailable", event_time="11:00", engineer_id="e1",
        unavailable_from="12:00", unavailable_until="12:00",
    )
    with pytest.raises(ValueError, match="позже"):
        blackout_for_event(event)


def test_blackout_for_event_is_none_for_other_types():
    assert blackout_for_event(ReplanEvent(type="cancel_order", event_time="11:00", order_id="1")) is None


def test_load_event_engineer_unavailable_reads_from_and_until(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {"type": "engineer_unavailable", "event_time": "11:00", "engineer_id": "eng_03",
             "from": "12:00", "until": "13:30"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    event = load_event(path)
    assert event.unavailable_from == "12:00"
    assert event.unavailable_until == "13:30"


def test_load_event_engineer_unavailable_rejects_until_before_from(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {"type": "engineer_unavailable", "event_time": "11:00", "engineer_id": "eng_03",
             "from": "13:00", "until": "12:00"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="позже"):
        load_event(path)


def _interval_fixture():
    # e1: "1" стартует 11:00 (50 мин, доделывается до 11:50), "2" в 12:00, "3" в ~12:30.
    # Окно недоступности [11:30, 12:10) выбивает только "2".
    orders = [
        Order("1", 55.701, 37.601, 50, "11:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 30, "12:00", "12:05", "Обычная", "Локальные работы", None),
        Order("3", 55.703, 37.603, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Уходит", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment(
        [_route("e1", "Уходит", [_stop("1", "11:00"), _stop("2", "12:00"), _stop("3", "12:30")])]
    )
    return orders, engineers, assignment


def test_apply_engineer_unavailable_interval_moves_only_orders_starting_in_window(tmp_path):
    orders, engineers, assignment = _interval_fixture()
    event = ReplanEvent(
        type="engineer_unavailable", event_time="08:30", engineer_id="e1",
        unavailable_from="11:30", unavailable_until="12:10",
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert [(r["order_id"], r["from_engineer_id"], r["to_engineer_id"]) for r in diff["reassigned"]] == [
        ("2", "e1", "e2")
    ]
    e1 = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert [s["order_id"] for s in e1["stops"]] == ["1", "3"]
    # "3" освободилась раньше окна конца, но начинаться внутри окна не может
    assert next(s for s in e1["stops"] if s["order_id"] == "3")["arrival"] == "12:10"
    e2 = next(r for r in plan["routes"] if r["engineer_id"] == "e2")
    assert [s["order_id"] for s in e2["stops"]] == ["2"]


def test_apply_engineer_unavailable_interval_keeps_orders_before_window(tmp_path):
    orders, engineers, assignment = _interval_fixture()
    event = ReplanEvent(
        type="engineer_unavailable", event_time="08:30", engineer_id="e1",
        unavailable_from="13:00", unavailable_until="14:00",
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["reassigned"] == []
    assert diff["routes_changed"] == []


def test_apply_engineer_unavailable_without_until_still_moves_all_open_orders(tmp_path):
    orders, engineers, assignment = _interval_fixture()
    event = ReplanEvent(type="engineer_unavailable", event_time="08:30", engineer_id="e1")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert {r["order_id"] for r in diff["reassigned"]} == {"1", "2", "3"}
    assert all(r["engineer_id"] != "e1" for r in plan["routes"])


def test_apply_event_uses_blackouts_from_previous_events(tmp_path):
    orders = [Order("N", 55.701, 37.601, 10, "09:00", "09:30", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Один", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([])
    event = ReplanEvent(
        type="new_urgent_order", event_time="08:30",
        new_order=Order("U", 55.702, 37.602, 10, "09:00", "09:30", "Срочная", "Локальные работы", None),
    )
    window = {"e1": [(parse_hhmm("09:00"), parse_hhmm("10:00"))]}

    _, blocked = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path), blackouts=window)
    _, free = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    assert [u["order_id"] for u in blocked["newly_unassigned"]] == ["U"]
    assert [a["order_id"] for a in free["newly_assigned"]] == ["U"]


def test_orphan_returning_to_same_engineer_is_resimulated_after_window(tmp_path):
    # Единственный инженер: заявка "2" стартует в 12:00 внутри окна [11:30, 12:10),
    # уходит в сироты и может вернуться только к нему же — уже после окна.
    orders = [
        Order("1", 55.701, 37.601, 50, "11:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 30, "12:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "Один", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([_route("e1", "Один", [_stop("1", "11:00"), _stop("2", "12:00")])])
    event = ReplanEvent(
        type="engineer_unavailable", event_time="08:30", engineer_id="e1",
        unavailable_from="11:30", unavailable_until="12:10",
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    e1 = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert [(s["order_id"], s["arrival"]) for s in e1["stops"]] == [("1", "11:00"), ("2", "12:10")]
    assert [(r["from_engineer_id"], r["to_engineer_id"], r["arrival"]) for r in diff["reassigned"]] == [
        ("e1", "e1", "12:10")
    ]


@pytest.mark.parametrize("bad", ["25:00", "12:60", "abc", 1200, None])
def test_blackout_for_event_rejects_malformed_bounds_with_value_error(bad):
    event = ReplanEvent(
        type="engineer_unavailable", event_time="11:00", engineer_id="e1", unavailable_from=bad,
    )
    if bad is None:
        assert blackout_for_event(event) is not None
    else:
        with pytest.raises(ValueError, match="ЧЧ:ММ"):
            blackout_for_event(event)


def test_apply_new_urgent_order_never_scheduled_before_event_time(tmp_path):
    # Срочная заявка не получает время раньше самого события, даже если
    # её окно открылось раньше.
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")]
    assignment = _assignment([])
    event = ReplanEvent(
        type="new_urgent_order", event_time="15:00",
        new_order=Order("urgent", 55.701, 37.601, 30, "09:00", "18:00", "Срочная", "Локальные работы", None),
    )

    plan, diff = apply_event(event, [], engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["newly_assigned"][0]["arrival"] >= "15:00"


def test_apply_event_never_rewrites_already_happened_stop(tmp_path):
    # Уже состоявшаяся остановка не меняется, даже если изменён хвост
    # того же маршрута.
    orders = [
        Order("past", 55.701, 37.601, 10, "08:00", "18:00", "Обычная", "Локальные работы", None),
        Order("cancel_me", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    past_stop = _stop("past", "10:00", travel_min=7, distance_km=3.5, reason="исходная")
    assignment = _assignment([_route("e1", "Тест", [past_stop, _stop("cancel_me", "11:00")])])
    event = ReplanEvent(type="cancel_order", event_time="11:00", order_id="cancel_me")

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    e1_route = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert e1_route["stops"] == [past_stop]  # байт-в-байт исходная запись, без пересимуляции


def test_apply_new_urgent_order_evicts_not_started_normal_work(tmp_path):
    # ТЗ §2.4.1: срочная заявка имеет приоритет над ещё не начатой обычной,
    # если иначе не влезает.
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "10:00", ["Локальные работы"], "Пешеход")]
    normal = Order("normal", 55.701, 37.601, 60, "09:00", "10:00", "Обычная", "Локальные работы", None)
    assignment = _assignment([_route("e1", "Тест", [_stop("normal", "09:00", travel_min=0, distance_km=0.0)])])
    event = ReplanEvent(
        type="new_urgent_order", event_time="09:00",
        new_order=Order("urgent", 55.701, 37.601, 60, "09:00", "09:30", "Срочная", "Локальные работы", None),
    )

    plan, diff = apply_event(event, [normal], engineers, OFFICE, assignment, _builder(tmp_path))

    assigned_ids = {s["order_id"] for r in plan["routes"] for s in r["stops"]}
    assert "urgent" in assigned_ids


def test_apply_engineer_unavailable_noop_does_not_resimulate_against_different_matrix(tmp_path):
    # Окно недоступности, не задевающее ни одной остановки, ничего не меняет
    # и не пересчитывает маршрут.
    orders = [Order("far", 55.90, 37.60, 10, "00:00", "23:59", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    # Записанные 11:00 при пересчёте по haversine недостижимы (office -> far
    # — часы пешком), поэтому пересчёт хвоста дал бы ValueError.
    assignment = _assignment([_route("e1", "Тест", [_stop("far", "11:00", travel_min=5, distance_km=1.0)])])
    # Окно недоступности 18:00-19:00 не пересекается со стартом "far" (11:00).
    event = ReplanEvent(
        type="engineer_unavailable", event_time="10:00", engineer_id="e1",
        unavailable_from="18:00", unavailable_until="19:00",
    )

    plan, diff = apply_event(event, orders, engineers, OFFICE, assignment, _builder(tmp_path))

    e1_route = next(r for r in plan["routes"] if r["engineer_id"] == "e1")
    assert e1_route["stops"][0]["arrival"] == "11:00"  # исходные данные как есть, без пересимуляции
    assert diff["reassigned"] == []
    assert diff["routes_changed"] == []


def test_urgent_order_takes_earliest_slot_not_cheapest_mileage(tmp_path):
    # Срочная заявка ставится на самое раннее допустимое время: e1 занят до
    # ~15:05 (и «дешевле» по пробегу, т.к. рядом), e2 свободен и приедет
    # около 12:20 — срочная должна достаться e2.
    busy = Order("busy", 55.745, 37.600, 240, "11:05", "11:10", "Обычная", "Локальные работы", None)
    engineers = [
        Engineer("e1", "Занят", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободен", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment([_route("e1", "Занят", [_stop("busy", "11:05")])])
    event = ReplanEvent(
        type="new_urgent_order", event_time="11:00",
        new_order=Order("urgent", 55.7451, 37.6001, 30, "11:00", "18:00", "Срочная", "Локальные работы", None),
    )

    plan, diff = apply_event(event, [busy], engineers, OFFICE, assignment, _builder(tmp_path))

    placed = diff["newly_assigned"][0]
    assert placed["engineer_id"] == "e2"
    assert "11:00" <= placed["arrival"] < "13:00"


def test_normal_new_order_still_uses_cheapest_mileage(tmp_path):
    # Та же ситуация, но заявка обычная: выбирается минимальный пробег (e1).
    busy = Order("busy", 55.745, 37.600, 240, "11:05", "11:10", "Обычная", "Локальные работы", None)
    engineers = [
        Engineer("e1", "Занят", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Свободен", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    assignment = _assignment([_route("e1", "Занят", [_stop("busy", "11:05")])])
    event = ReplanEvent(
        type="new_urgent_order", event_time="11:00",
        new_order=Order("regular", 55.7451, 37.6001, 30, "11:00", "18:00", "Обычная", "Локальные работы", None),
    )

    plan, diff = apply_event(event, [busy], engineers, OFFICE, assignment, _builder(tmp_path))

    assert diff["newly_assigned"][0]["engineer_id"] == "e1"


def test_replan_unassigned_reason_does_not_claim_busy_for_unavailable_engineer(tmp_path):
    # Единственный подходящий инженер недоступен до конца дня (окно из
    # предыдущего события сессии): причина не должна говорить «заняты
    # другими заявками» или «не успевает доехать».
    engineers = [Engineer("e1", "Один", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    event = ReplanEvent(
        type="new_urgent_order", event_time="11:00",
        new_order=Order("urgent", 55.701, 37.601, 30, "11:00", "18:00", "Срочная", "Локальные работы", None),
    )

    plan, diff = apply_event(
        event, [], engineers, OFFICE, _assignment([]), _builder(tmp_path), blackouts={"e1": [(0, 1440)]}
    )

    reason = diff["newly_unassigned"][0]["reason"]
    assert "заняты другими заявками" not in reason
    assert "Не успевает доехать" not in reason
