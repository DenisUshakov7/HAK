"""CLI-оркестратор: сырые CSV/JSON -> data/output/orders.json для планировщика."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Protocol

from etl.parse_raw import (
    RawOrder,
    ReferenceData,
    load_historical_control,
    load_reference,
    load_synthetic_orders,
)
from geocode import Geocoder, GeocodeResult

logger = logging.getLogger(__name__)


class GeocoderLike(Protocol):
    """Структурный контракт геокодера: в проде это geocode.Geocoder,
    в тестах — любой фейк с таким же методом."""

    def geocode_address(self, address: str, district: str | None = None) -> GeocodeResult: ...


def build_order_record(
    raw: RawOrder,
    geocoder: GeocoderLike,
    reference: ReferenceData,
    status: str | None = None,
    brigade: str | None = None,
) -> dict:
    """Собирает каноническую запись "Заявка" по схеме из брифа (§2.4)."""
    norm = reference.norms.get(raw.bk_type)
    if norm is None:
        logger.warning(
            "Неизвестный тип заявки BK %r для заявки %s — маппинг норматива невозможен",
            raw.bk_type,
            raw.order_id,
        )

    geocoded = geocoder.geocode_address(raw.address, raw.district)

    record = {
        "id": raw.order_id,
        "address": raw.address,
        "district": raw.district,
        "lat": geocoded.lat,
        "lon": geocoded.lon,
        "geocode_source": geocoded.source,
        "duration_min": norm.duration_on_site_min if norm else None,
        "window_start": raw.start_dt.strftime("%H:%M"),
        "window_end": raw.end_dt.strftime("%H:%M"),
        "date": raw.start_dt.strftime("%Y-%m-%d"),
        "priority": norm.priority if norm else "Обычная",
        "required_skill": norm.skill_name_ru if norm else None,
        "required_transport": None,
        "gigabit": raw.gigabit,
        "source_file": raw.source_file,
    }
    if status is not None:
        record["status"] = status
    if brigade is not None:
        record["historical_brigade"] = brigade
    return record


ORDER_CSV_FIELDS = [
    "id", "address", "district", "lat", "lon", "geocode_source",
    "duration_min", "window_start", "window_end", "date", "priority",
    "required_skill", "required_transport", "gigabit", "source_file",
]


def _write_json(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(records: list[dict], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig (BOM), а не utf-8 — иначе Excel открывает кириллицу в CSV
    # как кракозябры, а именно так этот файл скорее всего откроют вручную.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def run(
    raw_dir: Path,
    reference_dir: Path,
    output_dir: Path,
    no_network: bool,
    limit: int | None,
) -> dict:
    # glob() на несуществующей директории вернёт пустой список без ошибки —
    # без проверки опечатка в пути дала бы «успешный» прогон с нулём заявок.
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Директория с сырыми заявками не найдена: {raw_dir}")
    historical_dir = reference_dir / "historical"
    if not historical_dir.is_dir():
        raise FileNotFoundError(f"Директория с историческими данными не найдена: {historical_dir}")

    reference = load_reference(reference_dir)
    synthetic_orders, offices = load_synthetic_orders(raw_dir)
    historical_orders = load_historical_control(historical_dir)

    if limit is not None:
        synthetic_orders = synthetic_orders[:limit]

    geocoder = Geocoder(
        cache_path=reference_dir / "geocode_cache.json",
        fallback_path=reference_dir / "geocode_fallback.json",
        force_fallback=no_network,
    )

    order_records = [build_order_record(o, geocoder, reference) for o in synthetic_orders]
    historical_records = [
        build_order_record(o, geocoder, reference, status=o.status, brigade=o.brigade)
        for o in historical_orders
    ]

    office_records = []
    for office in offices:
        geocoded = geocoder.geocode_address(office.address)
        office_records.append(
            {
                "source_file": office.source_file,
                "address": office.address,
                "lat": geocoded.lat,
                "lon": geocoded.lon,
                "geocode_source": geocoded.source,
            }
        )

    _write_json(order_records, output_dir / "orders.json")
    # CSV — только для основного входа планировщика (синтетические заявки).
    # historical_reference и offices — справочные данные, им достаточно JSON.
    _write_csv(order_records, output_dir / "orders.csv", ORDER_CSV_FIELDS)
    _write_json(office_records, output_dir / "offices.json")
    _write_json(historical_records, output_dir / "historical_reference.json")

    # Офисы включены в сводку геокодирования наравне с заявками: адрес
    # офиса — стартовая точка инженеров, деградация до fallback там не менее
    # важна для видимости, чем для обычной заявки.
    all_records = order_records + historical_records + office_records
    return {
        "synthetic_orders": len(order_records),
        "historical_orders": len(historical_records),
        "offices": len(office_records),
        "geocoded_nominatim": sum(1 for r in all_records if r["geocode_source"] == "nominatim"),
        "geocoded_cache": sum(1 for r in all_records if r["geocode_source"] == "cache"),
        "geocoded_fallback": sum(1 for r in all_records if r["geocode_source"].startswith("fallback")),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать data/output/orders.json из сырых выгрузок."
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--reference-dir", type=Path, default=Path("data/reference"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/output"))
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Не ходить в Nominatim, использовать только fallback-координаты.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать только первые N заявок из синтетики (для быстрой отладки).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        summary = run(
            raw_dir=args.raw_dir,
            reference_dir=args.reference_dir,
            output_dir=args.output_dir,
            no_network=args.no_network,
            limit=args.limit,
        )
    except FileNotFoundError as exc:
        # run() уже формирует понятное сообщение — не показываем traceback.
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
