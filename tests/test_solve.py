from routing.distance_matrix import MatrixResult, haversine_km
from routing.models import Engineer, Order
from routing.solve import solve


def _matrix(points: list[tuple[float, float]], speed_kmh: float = 30.0) -> MatrixResult:
    distance_km = {}
    duration_min = {}
    for i, a in enumerate(points):
        for j, b in enumerate(points):
            km = haversine_km(a, b)
            distance_km[(i, j)] = km
            duration_min[(i, j)] = km / speed_kmh * 60
    return MatrixResult(distance_km=distance_km, duration_min=duration_min, source="test")


OFFICE = (55.70, 37.60)


def _route_for(result, engineer_index):
    return next((r for r in result.routes if r.engineer_index == engineer_index), None)


def test_solve_respects_skill_qualification():
    orders = [
        Order("1", 55.705, 37.605, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.706, 37.606, 10, "09:00", "18:00", "Обычная", "Аварийные работы", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "B", *OFFICE, "08:00", "20:00", ["Аварийные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    route_e1 = _route_for(result, 0)
    route_e2 = _route_for(result, 1)
    assert [s.order_index for s in route_e1.stops] == [0]
    assert [s.order_index for s in route_e2.stops] == [1]


def test_solve_respects_required_transport():
    orders = [
        Order("1", 55.705, 37.605, 10, "09:00", "18:00", "Обычная", "Локальные работы", "Автомобиль"),
    ]
    engineers = [
        Engineer("e1", "Пеший", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Авто", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Автомобиль"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    foot_matrix = _matrix(points, speed_kmh=5.0)
    # Пеший инженер сделан заведомо "дешевле" по дуге (в 100 раз короче
    # расстояние), чем требуется транспортом. Если бы фильтр по
    # required_transport не работал, solver выбрал бы именно его как более
    # дешёвый вариант — раздельные (не совпадающие по цене) матрицы делают
    # тест реальной проверкой ограничения, а не случайным совпадением.
    foot_matrix = MatrixResult(
        distance_km={key: value / 100 for key, value in foot_matrix.distance_km.items()},
        duration_min=foot_matrix.duration_min,
        source="test",
    )
    matrices = {
        "foot": foot_matrix,
        "car": _matrix(points, speed_kmh=30.0),
    }

    result = solve(orders, engineers, matrices)

    assert result.unassigned_order_indices == []
    assert _route_for(result, 0) is None
    assert [s.order_index for s in _route_for(result, 1).stops] == [0]


def test_solve_rejects_order_outside_any_shift():
    orders = [
        Order("1", 55.705, 37.605, 10, "14:00", "16:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "12:00", ["Локальные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices, time_limit_seconds=3)

    assert result.unassigned_order_indices == [0]
    assert result.routes == []


def test_solve_waits_for_window_to_open():
    orders = [
        Order("1", 55.705, 37.605, 10, "11:00", "12:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices, time_limit_seconds=3)

    stop = _route_for(result, 0).stops[0]
    assert 11 * 60 <= stop.arrival_min <= 12 * 60


def test_solve_prefers_fewer_engineers_when_feasible():
    orders = [
        Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "B", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices, time_limit_seconds=3)

    assert result.unassigned_order_indices == []
    assert len(result.routes) == 1
    assert {s.order_index for s in result.routes[0].stops} == {0, 1}


def test_solve_leaves_truly_impossible_order_unassigned_without_crashing():
    orders = [
        Order("1", 55.705, 37.605, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.706, 37.606, 10, "09:00", "18:00", "Обычная", "Секретный навык", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices, time_limit_seconds=3)

    assert result.unassigned_order_indices == [1]
    assert [s.order_index for s in result.routes[0].stops] == [0]


def test_solve_computes_correct_distance_for_single_stop():
    orders = [
        Order("1", 55.705, 37.605, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    points = [OFFICE] + [(o.lat, o.lon) for o in orders]
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve(orders, engineers, matrices, time_limit_seconds=3)

    stop = result.routes[0].stops[0]
    expected_km = matrices["foot"].distance_km[(0, 1)]
    assert stop.distance_km == expected_km
    assert result.routes[0].total_distance_km == expected_km


def test_solve_rejects_unreachable_arc_instead_of_impossible_schedule():
    # Недостижимая дуга (inf) не должна давать невозможный маршрут: solver
    # просто не использует её.
    orders = [
        Order("1", 55.701, 37.601, 40, "08:00", "09:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 40, "08:00", "09:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "09:00", ["Локальные работы"], "Пешеход")]
    distances = {(i, j): 1.0 for i in range(3) for j in range(3) if i != j}
    durations = {(i, j): 10.0 for i in range(3) for j in range(3) if i != j}
    for i in range(3):
        distances[(i, i)] = 0.0
        durations[(i, i)] = 0.0
    distances[(0, 1)] = float("inf")  # депо -> заявка "1" недостижимо
    durations[(0, 1)] = float("inf")
    matrix = MatrixResult(distance_km=distances, duration_min=durations, source="test")

    result = solve(orders, engineers, {"foot": matrix}, time_limit_seconds=1)

    for route in result.routes:
        previous_finish = 480  # 08:00
        for stop in route.stops:
            assert stop.arrival_min >= previous_finish + stop.travel_min
            previous_finish = stop.arrival_min + 40
        assert previous_finish <= 540  # 09:00


def test_solve_prefers_leaving_lower_priority_tier_unassigned_when_resources_scarce():
    # При нехватке ресурсов приоритет: Авария > Подключение > Ремонт/Дозаказ.
    # Обе заявки вместе не влезают в смену — без назначения остаётся менее
    # приоритетная («Локальные работы»).
    engineer = Engineer(
        "e1", "A", *OFFICE, "08:00", "09:00",
        ["Работы на подключение и дозаказы", "Локальные работы"], "Пешеход",
    )
    connect = Order(
        "connect", *OFFICE, 55, "08:00", "18:00", "Обычная", "Работы на подключение и дозаказы", None
    )
    local = Order("local", *OFFICE, 55, "08:00", "18:00", "Обычная", "Локальные работы", None)
    points = [OFFICE, OFFICE, OFFICE]  # office+обе заявки в одной точке -> нулевой пробег/время
    matrices = {"foot": _matrix(points, speed_kmh=5.0)}

    result = solve([connect, local], [engineer], matrices)

    assert result.unassigned_order_indices == [1]  # "local" (индекс 1) не назначена
    assigned_ids = {s.order_index for r in result.routes for s in r.stops}
    assert assigned_ids == {0}  # "connect" (индекс 0) назначена


def test_unassigned_penalty_warns_on_unknown_skill(caplog):
    # Неизвестный навык получает запасной штраф и предупреждение в логе.
    from routing.solve import _unassigned_penalty

    order = Order("1", *OFFICE, 10, "09:00", "18:00", "Обычная", "Совсем новый навык", None)

    with caplog.at_level("WARNING"):
        penalty = _unassigned_penalty(order)

    assert penalty == 50_000_000
    assert any("Совсем новый навык" in r.message for r in caplog.records)


def test_unassigned_penalty_no_warning_for_known_skill(caplog):
    from routing.solve import _unassigned_penalty

    order = Order("1", *OFFICE, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)

    with caplog.at_level("WARNING"):
        penalty = _unassigned_penalty(order)

    assert penalty == 30_000_000
    assert caplog.records == []
