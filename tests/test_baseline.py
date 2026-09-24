from routing.baseline import explain_baseline_assigned, solve_baseline
from routing.distance_matrix import MatrixResult
from routing.models import Engineer, Order

OFFICE = (55.70, 37.60)


def _matrix(pairs: dict[tuple[int, int], tuple[float, float]]) -> MatrixResult:
    """pairs: {(from_node, to_node): (distance_km, duration_min)} — только
    те пары, что реально понадобятся тесту (полная симметричная матрица не
    нужна, solve_baseline обращается только к парам "текущая точка ->
    следующая кандидатная заявка")."""
    distance_km = {key: value[0] for key, value in pairs.items()}
    duration_min = {key: value[1] for key, value in pairs.items()}
    return MatrixResult(distance_km=distance_km, duration_min=duration_min, source="test")


def _route_for(result, engineer_index):
    return next((r for r in result.routes if r.engineer_index == engineer_index), None)


def test_baseline_preserves_assignment_order_not_distance_order():
    # Три заявки одному инженеру: order 0 "далеко", order 1 "близко",
    # order 2 "средне" — если бы алгоритм оптимизировал порядок посещения,
    # он бы съездил сначала в 1, потом в 2, потом в 0. Базовый вариант
    # обязан сохранить порядок из orders.json: 0, 1, 2.
    orders = [
        Order("far", 55.90, 37.60, 10, "08:00", "20:00", "Обычная", "Локальные работы", None),
        Order("near", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Локальные работы", None),
        Order("mid", 55.75, 37.60, 10, "08:00", "20:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    matrices = {
        "foot": _matrix({
            (0, 1): (22.0, 30.0), (1, 2): (22.0, 30.0), (2, 3): (5.0, 10.0),
        })
    }

    result = solve_baseline(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    route = _route_for(result, 0)
    assert [s.order_index for s in route.stops] == [0, 1, 2]


def test_baseline_skips_engineer_without_skill():
    orders = [Order("1", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Аварийные работы", None)]
    engineers = [
        Engineer("e1", "Без навыка", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "С навыком", *OFFICE, "08:00", "20:00", ["Аварийные работы"], "Пешеход"),
    ]
    matrices = {"foot": _matrix({(0, 1): (1.0, 10.0)})}

    result = solve_baseline(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    assert _route_for(result, 0) is None
    assert [s.order_index for s in _route_for(result, 1).stops] == [0]


def test_baseline_skips_engineer_without_required_transport():
    orders = [
        Order("1", 55.701, 37.601, 10, "08:00", "20:00", "Обычная", "Локальные работы", "Автомобиль"),
    ]
    engineers = [
        Engineer("e1", "Пеший", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Авто", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Автомобиль"),
    ]
    matrices = {
        "foot": _matrix({(0, 1): (1.0, 10.0)}),
        "car": _matrix({(0, 1): (1.0, 10.0)}),
    }

    result = solve_baseline(orders, engineers, matrices)

    assert _route_for(result, 0) is None
    assert [s.order_index for s in _route_for(result, 1).stops] == [0]


def test_baseline_falls_back_to_next_engineer_when_first_is_busy():
    # order A (idx 0) и order B (idx 1) с одинаковым узким окном 09:00-09:30.
    # Инженер 0 берёт A первым (свободен, успевает), но дорога от A до B
    # намеренно огромная (50 мин) — B у него уже не влезает в окно. Инженер 1
    # (тоже подходит по навыку) ещё свободен с самого офиса и успевает на B.
    orders = [
        Order("A", 55.701, 37.601, 10, "09:00", "09:30", "Обычная", "Локальные работы", None),
        Order("B", 55.702, 37.602, 10, "09:00", "09:30", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Первый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Второй", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    matrices = {
        "foot": _matrix({
            (0, 1): (1.0, 5.0),   # офис -> A
            (0, 2): (1.0, 5.0),   # офис -> B
            (1, 2): (10.0, 50.0),  # A -> B (намеренно долго)
        })
    }

    result = solve_baseline(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    assert [s.order_index for s in _route_for(result, 0).stops] == [0]
    assert [s.order_index for s in _route_for(result, 1).stops] == [1]


def test_baseline_leaves_order_unassigned_when_only_engineer_is_busy():
    orders = [
        Order("A", 55.701, 37.601, 10, "09:00", "09:30", "Обычная", "Локальные работы", None),
        Order("B", 55.702, 37.602, 10, "09:00", "09:30", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "Единственный", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    matrices = {
        "foot": _matrix({
            (0, 1): (1.0, 5.0),
            (0, 2): (1.0, 5.0),
            (1, 2): (10.0, 50.0),
        })
    }

    result = solve_baseline(orders, engineers, matrices)

    assert result.unassigned_order_indices == [1]
    assert [s.order_index for s in _route_for(result, 0).stops] == [0]


def test_baseline_arrival_min_is_int_for_format_hhmm():
    # travel_min округляется до сложения: иначе arrival_min станет float и
    # format_hhmm() упадёт.
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "20:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    matrices = {"foot": _matrix({(0, 1): (1.23, 7.6)})}  # нецелое duration_min

    result = solve_baseline(orders, engineers, matrices)

    stop = _route_for(result, 0).stops[0]
    assert isinstance(stop.arrival_min, int)
    assert isinstance(stop.travel_min, int)


def test_baseline_only_requires_start_within_window_not_completion():
    # Окно ограничивает только начало работы (ТЗ §2.2), завершение — смена:
    # окно 09:00-09:05 при длительности 10 минут допустимо.
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "09:05", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "18:00", ["Локальные работы"], "Пешеход")]
    matrices = {"foot": _matrix({(0, 1): (0.1, 1.0)})}

    result = solve_baseline(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    assert [s.order_index for s in _route_for(result, 0).stops] == [0]


def test_baseline_empty_orders_returns_no_routes():
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]

    result = solve_baseline([], engineers, {})

    assert result.routes == []
    assert result.unassigned_order_indices == []


def test_explain_baseline_assigned_mentions_no_optimization():
    order = Order("1", 55.701, 37.601, 10, "09:00", "20:00", "Обычная", "Локальные работы", None)
    engineer = Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")

    reason = explain_baseline_assigned(order, engineer, [engineer])

    assert "базов" in reason.lower()


def test_baseline_skips_unreachable_arc_instead_of_crashing():
    # Недостижимая дуга (inf) не должна ронять baseline: заявка достаётся
    # следующему подходящему инженеру. У инженеров разные профили
    # (пеший/авто), поэтому у второго своя конечная матрица.
    orders = [Order("1", 55.701, 37.601, 10, "08:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [
        Engineer("e1", "Недостижим", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Достижим", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Автомобиль"),
    ]
    unreachable = MatrixResult(
        distance_km={(0, 1): float("inf")}, duration_min={(0, 1): float("inf")}, source="test"
    )
    reachable = MatrixResult(distance_km={(0, 1): 1.0}, duration_min={(0, 1): 10.0}, source="test")

    result = solve_baseline(orders, engineers, {"foot": unreachable, "car": reachable})

    assert result.unassigned_order_indices == []
    route_e2 = _route_for(result, 1)
    assert route_e2 is not None and [s.order_index for s in route_e2.stops] == [0]
