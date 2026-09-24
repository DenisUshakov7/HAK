"""CLI: считает и оптимальный (OR-Tools), и базовый (жадный, ТЗ §2.3)
план на одних и тех же данных/матрицах, пишет их сравнение в
data/output/comparison.json. Не читает и не трогает assignment.json —
оба плана считаются заново в одном процессе, чтобы сравнение было
честным (без дрейфа между старым прогоном OR-Tools и свежим базовым)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from routing.baseline import explain_baseline_assigned, solve_baseline
from routing.distance_matrix import DEFAULT_CACHE_PATH, VEHICLE_TO_PROFILE, DistanceMatrixBuilder
from routing.explain import explain_assigned
from routing.models import load_engineers, load_office_coords, load_orders
from routing.plan_output import build_plan_output
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

    optimized_result = solve(orders, engineers, matrices, time_limit_seconds=time_limit_seconds)
    baseline_result = solve_baseline(orders, engineers, matrices)

    optimized_output = build_plan_output(optimized_result, orders, engineers, explain_assigned)
    baseline_output = build_plan_output(baseline_result, orders, engineers, explain_baseline_assigned)

    output = {
        "optimized": optimized_output,
        "baseline": baseline_output,
        "comparison": {
            metric: {
                "optimized": optimized_output["metrics"][metric],
                "baseline": baseline_output["metrics"][metric],
            }
            for metric in (
                "engineers_used",
                "total_distance_km",
                "orders_assigned",
                "orders_unassigned",
            )
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _format_comparison_table(comparison: dict) -> str:
    rows = [
        ("Задействовано инженеров:", "engineers_used"),
        ("Суммарный пробег, км:", "total_distance_km"),
        ("Назначено заявок:", "orders_assigned"),
        ("Не назначено заявок:", "orders_unassigned"),
    ]
    lines = [
        "Сравнение с базовым вариантом (без оптимизации):",
        f"{'':<26}{'Оптимизация':>15}{'Базовый вариант':>18}",
    ]
    for label, key in rows:
        lines.append(f"{label:<26}{comparison[key]['optimized']:>15}{comparison[key]['baseline']:>18}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сравнить оптимизированный и базовый (ТЗ §2.3) планы распределения."
    )
    parser.add_argument("--orders-path", type=Path, default=Path("data/output/orders.json"))
    parser.add_argument("--engineers-path", type=Path, default=Path("data/output/engineers.json"))
    parser.add_argument("--offices-path", type=Path, default=Path("data/output/offices.json"))
    parser.add_argument("--output", type=Path, default=Path("data/output/comparison.json"))
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
    print(_format_comparison_table(output["comparison"]))


if __name__ == "__main__":
    main()
