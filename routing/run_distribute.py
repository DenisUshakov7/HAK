"""CLI: читает data/output/{orders,engineers,offices}.json, строит
матрицы расстояний и решение, пишет data/output/assignment.json."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from routing.distance_matrix import DEFAULT_CACHE_PATH, VEHICLE_TO_PROFILE, DistanceMatrixBuilder
from routing.explain import explain_assigned, explain_unassigned, office_travel_fn
from routing.models import format_hhmm, load_engineers, load_office_coords, load_orders
from routing.solve import solve

logger = logging.getLogger(__name__)


def run(
    orders_path: Path,
    engineers_path: Path,
    offices_path: Path,
    output_path: Path,
    force_fallback: bool = False,
    time_limit_seconds: int = 10,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> dict:
    orders = load_orders(orders_path)
    engineers = load_engineers(engineers_path)
    office = load_office_coords(offices_path)

    if not orders:
        raise ValueError(f"В {orders_path} нет ни одной заявки")
    if not engineers:
        raise ValueError(f"В {engineers_path} нет ни одного инженера")

    points = [office] + [(o.lat, o.lon) for o in orders]
    profiles_needed = {VEHICLE_TO_PROFILE[e.vehicle] for e in engineers}
    builder = DistanceMatrixBuilder(cache_path=cache_path, force_fallback=force_fallback)
    matrices = {profile: builder.build(profile, points) for profile in profiles_needed}

    result = solve(orders, engineers, matrices, time_limit_seconds=time_limit_seconds)

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
                "reason": explain_assigned(orders[stop.order_index], engineer, engineers),
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
        {
            "order_id": orders[idx].id,
            "reason": explain_unassigned(
                orders[idx], engineers, office_travel_fn(matrices, 0, idx + 1)
            ),
        }
        for idx in result.unassigned_order_indices
    ]

    output = {
        "routes": routes_out,
        "unassigned": unassigned_out,
        "metrics": {
            "engineers_used": engineers_used,
            "total_distance_km": round(total_distance_km, 2),
            "orders_assigned": len(orders) - len(unassigned_out),
            "orders_unassigned": len(unassigned_out),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Распределить заявки между инженерами и построить маршруты."
    )
    parser.add_argument("--orders-path", type=Path, default=Path("data/output/orders.json"))
    parser.add_argument("--engineers-path", type=Path, default=Path("data/output/engineers.json"))
    parser.add_argument("--offices-path", type=Path, default=Path("data/output/offices.json"))
    parser.add_argument("--output", type=Path, default=Path("data/output/assignment.json"))
    parser.add_argument(
        "--no-network", action="store_true", help="Не ходить в OpenRouteService, только haversine-фолбэк"
    )
    parser.add_argument("--time-limit", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = _parse_args(argv)
    try:
        # Ожидаемые ошибки: нет файла, пустой вход, OR-Tools не нашёл решения.
        # Ловим их явно, а не bare except, чтобы не проглотить настоящий баг.
        output = run(
            orders_path=args.orders_path,
            engineers_path=args.engineers_path,
            offices_path=args.offices_path,
            output_path=args.output,
            force_fallback=args.no_network,
            time_limit_seconds=args.time_limit,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(output["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
