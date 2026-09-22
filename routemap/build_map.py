"""CLI: строит статичную HTML-карту маршрутов поверх
data/output/assignment.json (см. routing/run_distribute.py)."""
from __future__ import annotations

import argparse
import html as html_lib
import json
import sys
from pathlib import Path

import folium
from dotenv import load_dotenv

from routing.distance_matrix import (
    DEFAULT_CACHE_PATH,
    VEHICLE_TO_PROFILE,
    DistanceMatrixBuilder,
)
from routing.models import Engineer, Order, load_engineers, load_office_coords, load_orders

PALETTE = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe",
    "#008080", "#e6beff",
]


def _build_comparison_block(comparison: dict) -> str:
    rows = [
        ("Задействовано инженеров", "engineers_used"),
        ("Суммарный пробег, км", "total_distance_km"),
        ("Назначено заявок", "orders_assigned"),
        ("Не назначено заявок", "orders_unassigned"),
    ]
    table_rows = "".join(
        f"<tr><td>{html_lib.escape(label)}</td>"
        f"<td>{comparison[key]['optimized']}</td>"
        f"<td>{comparison[key]['baseline']}</td></tr>"
        for label, key in rows
    )
    return (
        "<h3>Сравнение с базовым вариантом (без оптимизации)</h3>"
        "<table border='1' cellpadding='4' style='border-collapse:collapse;font-size:12px;'>"
        "<tr><th></th><th>Оптимизация</th><th>Базовый вариант</th></tr>"
        f"{table_rows}</table>"
    )


def _build_legend(assignment: dict, engineers: list[Engineer], comparison: dict | None = None) -> str:
    metrics = assignment["metrics"]
    route_blocks = []
    for i, route in enumerate(assignment["routes"]):
        color = PALETTE[i % len(PALETTE)]
        stop_rows = "".join(
            f"<tr><td>{html_lib.escape(stop['order_id'])}</td>"
            f"<td>{html_lib.escape(stop['arrival'])}</td>"
            f"<td>{html_lib.escape(stop['reason'])}</td></tr>"
            for stop in route["stops"]
        )
        route_blocks.append(
            f'<h4><span style="display:inline-block;width:12px;height:12px;'
            f'background:{color};border-radius:50%;margin-right:6px;"></span>'
            f"{html_lib.escape(route['engineer_name'])} "
            f"({html_lib.escape(route['engineer_id'])}) — "
            f"{route['total_distance_km']} км, {len(route['stops'])} заявок</h4>"
            f"<table border='1' cellpadding='4' style='border-collapse:collapse;font-size:12px;'>"
            f"<tr><th>Заявка</th><th>Прибытие</th><th>Причина</th></tr>"
            f"{stop_rows}</table>"
        )

    unassigned_block = ""
    if assignment["unassigned"]:
        unassigned_rows = "".join(
            f"<tr><td>{html_lib.escape(u['order_id'])}</td>"
            f"<td>{html_lib.escape(u['reason'])}</td></tr>"
            for u in assignment["unassigned"]
        )
        unassigned_block = (
            "<h4>Неназначенные заявки</h4>"
            "<table border='1' cellpadding='4' style='border-collapse:collapse;font-size:12px;'>"
            "<tr><th>Заявка</th><th>Причина</th></tr>"
            f"{unassigned_rows}</table>"
        )

    used_ids = {route["engineer_id"] for route in assignment["routes"]}
    idle = [e for e in engineers if e.id not in used_ids]
    idle_block = ""
    if idle:
        idle_rows = "".join(
            f"<li>{html_lib.escape(e.name)} "
            f"({html_lib.escape(e.id)}, {html_lib.escape(e.vehicle)})</li>"
            for e in idle
        )
        idle_block = f"<h4>Инженеры без назначений</h4><ul>{idle_rows}</ul>"

    comparison_block = _build_comparison_block(comparison) if comparison is not None else ""

    return (
        '<div style="max-width:900px;margin:16px;font-family:sans-serif;">'
        "<h3>Сводка по плану</h3>"
        f"<p>Задействовано инженеров: {metrics['engineers_used']}; "
        f"суммарный пробег: {metrics['total_distance_km']} км; "
        f"назначено заявок: {metrics['orders_assigned']}; "
        f"не назначено: {metrics['orders_unassigned']}.</p>"
        + "".join(route_blocks)
        + unassigned_block
        + idle_block
        + comparison_block
        + "</div>"
    )


def _numbered_icon(seq: int, color: str) -> folium.DivIcon:
    return folium.DivIcon(
        html=(
            f'<div style="background-color:{color};color:white;'
            f"border-radius:50%;width:24px;height:24px;text-align:center;"
            f"line-height:24px;font-size:12px;font-weight:bold;"
            f'border:2px solid white;box-shadow:0 0 3px rgba(0,0,0,0.5);">'
            f"{seq}</div>"
        )
    )


def run(
    orders_path: Path,
    engineers_path: Path,
    offices_path: Path,
    assignment_path: Path,
    output_path: Path,
    force_fallback: bool = False,
    cache_path: Path = DEFAULT_CACHE_PATH,
    comparison_path: Path = Path("data/output/comparison.json"),
) -> Path:
    orders = load_orders(orders_path)
    engineers = load_engineers(engineers_path)
    office = load_office_coords(offices_path)
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))

    orders_by_id: dict[str, Order] = {o.id: o for o in orders}
    engineers_by_id: dict[str, Engineer] = {e.id: e for e in engineers}
    builder = DistanceMatrixBuilder(cache_path=cache_path, force_fallback=force_fallback)

    comparison = None
    if comparison_path.exists():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))["comparison"]

    def _order_or_raise(order_id: str) -> Order:
        order = orders_by_id.get(order_id)
        if order is None:
            raise ValueError(
                f"Заявка {order_id!r} есть в {assignment_path}, но отсутствует "
                f"в {orders_path} — assignment.json устарел, перегенерируйте его "
                f"(python -m routing.run_distribute)"
            )
        return order

    def _engineer_or_raise(engineer_id: str) -> Engineer:
        engineer = engineers_by_id.get(engineer_id)
        if engineer is None:
            raise ValueError(
                f"Инженер {engineer_id!r} есть в {assignment_path}, но отсутствует "
                f"в {engineers_path} — assignment.json устарел, перегенерируйте его "
                f"(python -m routing.run_distribute)"
            )
        return engineer

    m = folium.Map(location=office, zoom_start=12, tiles="OpenStreetMap")
    folium.Marker(
        location=office,
        icon=folium.Icon(icon="home", color="cadetblue"),
        popup="Офис (стартовая точка)",
    ).add_to(m)

    for i, route in enumerate(assignment["routes"]):
        color = PALETTE[i % len(PALETTE)]
        points = [office]
        for seq, stop in enumerate(route["stops"], start=1):
            order = _order_or_raise(stop["order_id"])
            points.append((order.lat, order.lon))
            popup_html = (
                f"<b>Заявка {html_lib.escape(stop['order_id'])}</b><br>"
                f"Инженер: {html_lib.escape(route['engineer_name'])}<br>"
                f"Прибытие: {html_lib.escape(stop['arrival'])}<br>"
                f"Пробег до этой точки: {stop['distance_km']} км<br>"
                f"{html_lib.escape(stop['reason'])}"
            )
            folium.Marker(
                location=(order.lat, order.lon),
                icon=_numbered_icon(seq, color),
                popup=folium.Popup(popup_html, max_width=300),
            ).add_to(m)

        engineer = _engineer_or_raise(route["engineer_id"])
        profile = VEHICLE_TO_PROFILE[engineer.vehicle]
        geometry = builder.fetch_geometry(profile, points)
        line_points = geometry if geometry is not None else points
        folium.PolyLine(line_points, color=color, weight=3, opacity=0.7).add_to(m)

    for u in assignment["unassigned"]:
        order = _order_or_raise(u["order_id"])
        popup_html = (
            f"<b>Заявка {html_lib.escape(u['order_id'])} — не назначена</b><br>"
            f"{html_lib.escape(u['reason'])}"
        )
        folium.Marker(
            location=(order.lat, order.lon),
            icon=folium.Icon(icon="remove", color="gray"),
            popup=folium.Popup(popup_html, max_width=300),
        ).add_to(m)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(output_path))

    legend_html = _build_legend(assignment, engineers, comparison)
    page = output_path.read_text(encoding="utf-8")
    if "</body>" in page:
        page = page.replace("</body>", legend_html + "</body>", 1)
    else:
        # Не должно происходить на текущей версии Folium (0.20.x всегда
        # закрывает <body>), но если формат вывода когда-нибудь изменится —
        # лучше дописать легенду в конец файла, чем молча её потерять.
        page += legend_html
    output_path.write_text(page, encoding="utf-8")

    return output_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Построить HTML-карту маршрутов поверх assignment.json."
    )
    parser.add_argument("--orders-path", type=Path, default=Path("data/output/orders.json"))
    parser.add_argument("--engineers-path", type=Path, default=Path("data/output/engineers.json"))
    parser.add_argument("--offices-path", type=Path, default=Path("data/output/offices.json"))
    parser.add_argument(
        "--assignment-path", type=Path, default=Path("data/output/assignment.json")
    )
    parser.add_argument("--output", type=Path, default=Path("data/output/route_map.html"))
    parser.add_argument(
        "--no-network", action="store_true", help="Не ходить в OpenRouteService, только прямые линии"
    )
    parser.add_argument(
        "--comparison-path", type=Path, default=Path("data/output/comparison.json")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = _parse_args(argv)
    try:
        output_path = run(
            orders_path=args.orders_path,
            engineers_path=args.engineers_path,
            offices_path=args.offices_path,
            assignment_path=args.assignment_path,
            output_path=args.output,
            force_fallback=args.no_network,
            comparison_path=args.comparison_path,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Карта сохранена: {output_path}")


if __name__ == "__main__":
    main()
