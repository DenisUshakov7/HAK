from routing.models import Engineer, Order
from routing.plan_output import build_plan_output
from routing.solve import SolveResult, StopResult, VehicleRoute

OFFICE = (55.70, 37.60)


def test_build_plan_output_shape_and_rounding():
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "Тест Тестов", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    result = SolveResult(
        routes=[
            VehicleRoute(
                engineer_index=0,
                stops=[StopResult(order_index=0, arrival_min=600, travel_min=5, distance_km=1.23456)],
                total_distance_km=1.23456,
            )
        ],
        unassigned_order_indices=[],
    )

    output = build_plan_output(result, orders, engineers, lambda o, e, es: "ПРИЧИНА-ТЕСТ")

    assert output["routes"][0]["engineer_id"] == "e1"
    assert output["routes"][0]["engineer_name"] == "Тест Тестов"
    assert output["routes"][0]["total_distance_km"] == 1.23
    stop = output["routes"][0]["stops"][0]
    assert stop["order_id"] == "1"
    assert stop["arrival"] == "10:00"
    assert stop["travel_min"] == 5
    assert stop["distance_km"] == 1.23
    assert stop["reason"] == "ПРИЧИНА-ТЕСТ"
    assert output["unassigned"] == []
    assert output["metrics"] == {
        "engineers_used": 1,
        "total_distance_km": 1.23,
        "orders_assigned": 1,
        "orders_unassigned": 0,
    }


def test_build_plan_output_uses_default_unassigned_reason():
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Секретный навык", None)]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    result = SolveResult(routes=[], unassigned_order_indices=[0])

    output = build_plan_output(result, orders, engineers, lambda o, e, es: "не используется")

    assert output["unassigned"] == [
        {"order_id": "1", "reason": "Нет ни одного инженера с навыком «Секретный навык»"}
    ]
    assert output["metrics"]["engineers_used"] == 0
    assert output["metrics"]["orders_unassigned"] == 1


def test_build_plan_output_uses_custom_unassigned_reason():
    orders = [Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None)]
    engineers = [Engineer("e1", "A", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход")]
    result = SolveResult(routes=[], unassigned_order_indices=[0])

    output = build_plan_output(
        result, orders, engineers,
        lambda o, e, es: "не используется",
        unassigned_reason=lambda o, es: "КАСТОМНАЯ ПРИЧИНА",
    )

    assert output["unassigned"] == [{"order_id": "1", "reason": "КАСТОМНАЯ ПРИЧИНА"}]


def test_build_plan_output_aggregates_multiple_routes_and_mixed_outcome():
    # Реалистичный случай для compare_baseline.py: несколько маршрутов (у
    # одного — несколько остановок), плюс неназначенная заявка в том же
    # результате. Проверяет, что суммирование engineers_used/
    # total_distance_km по нескольким маршрутам и agreed-функция реально
    # получают правильные order/engineer, а не просто прокидывают
    # захардкоженное значение (предыдущие тесты этого не проверяли).
    orders = [
        Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("3", 55.703, 37.603, 10, "09:00", "18:00", "Обычная", "Секретный навык", None),
    ]
    engineers = [
        Engineer("e1", "Первый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Второй", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Автомобиль"),
    ]
    result = SolveResult(
        routes=[
            VehicleRoute(
                engineer_index=0,
                stops=[
                    StopResult(order_index=0, arrival_min=600, travel_min=5, distance_km=1.0),
                    StopResult(order_index=1, arrival_min=620, travel_min=5, distance_km=2.0),
                ],
                total_distance_km=3.0,
            ),
            VehicleRoute(
                engineer_index=1,
                stops=[StopResult(order_index=2, arrival_min=650, travel_min=5, distance_km=4.0)],
                total_distance_km=4.0,
            ),
        ],
        unassigned_order_indices=[],
    )

    seen_calls = []

    def assigned_reason(order, engineer, all_engineers):
        seen_calls.append((order.id, engineer.id, len(all_engineers)))
        return f"причина-{order.id}-{engineer.id}"

    output = build_plan_output(result, orders, engineers, assigned_reason)

    assert seen_calls == [("1", "e1", 2), ("2", "e1", 2), ("3", "e2", 2)]
    assert [r["engineer_id"] for r in output["routes"]] == ["e1", "e2"]
    assert [s["order_id"] for s in output["routes"][0]["stops"]] == ["1", "2"]
    assert output["routes"][0]["stops"][1]["reason"] == "причина-2-e1"
    assert output["metrics"] == {
        "engineers_used": 2,
        "total_distance_km": 7.0,
        "orders_assigned": 3,
        "orders_unassigned": 0,
    }
