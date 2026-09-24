"""Сборка JSON-формата результата планирования (ТЗ §2.4.2) — общая для
любого алгоритма, возвращающего routing.solve.SolveResult. Используется
routing/compare_baseline.py для оптимизированного и базового планов;
routing/run_distribute.py собирает свой результат отдельно."""
from __future__ import annotations

from typing import Callable

from routing.explain import explain_unassigned
from routing.models import Engineer, Order, format_hhmm
from routing.solve import SolveResult


def build_plan_output(
    result: SolveResult,
    orders: list[Order],
    engineers: list[Engineer],
    assigned_reason: Callable[[Order, Engineer, list[Engineer]], str],
    unassigned_reason: Callable[[Order, list[Engineer]], str] = explain_unassigned,
) -> dict:
    routes_out = []
    engineers_used = 0
    total_distance_km = 0.0
    for route in result.routes:
        engineer = engineers[route.engineer_index]
        engineers_used += 1
        total_distance_km += route.total_distance_km
        stops_out = [
            {
                "order_id": orders[stop.order_index].id,
                "arrival": format_hhmm(stop.arrival_min),
                "travel_min": stop.travel_min,
                "distance_km": round(stop.distance_km, 2),
                "reason": assigned_reason(orders[stop.order_index], engineer, engineers),
            }
            for stop in route.stops
        ]
        routes_out.append(
            {
                "engineer_id": engineer.id,
                "engineer_name": engineer.name,
                "stops": stops_out,
                "total_distance_km": round(route.total_distance_km, 2),
            }
        )

    unassigned_out = [
        {"order_id": orders[idx].id, "reason": unassigned_reason(orders[idx], engineers)}
        for idx in result.unassigned_order_indices
    ]

    return {
        "routes": routes_out,
        "unassigned": unassigned_out,
        "metrics": {
            "engineers_used": engineers_used,
            "total_distance_km": round(total_distance_km, 2),
            "orders_assigned": len(orders) - len(unassigned_out),
            "orders_unassigned": len(unassigned_out),
        },
    }
