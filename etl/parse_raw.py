"""Чтение сырых выгрузок заявок и справочных данных в структуры Python.

Никакого геокодирования и сетевых вызовов здесь нет — это задача geocode.py.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATETIME_FORMAT = "%d.%m.%Y %H:%M"


@dataclass(frozen=True)
class NormEntry:
    road_min: int
    tech_min: int
    docs_min: int
    base_norm_min: int
    duration_on_site_min: int
    skill: str
    skill_name_ru: str
    priority: str


@dataclass(frozen=True)
class ReferenceData:
    norms: dict[str, NormEntry]
    fallback_centroids: dict[str, tuple[float, float]]
    fallback_default: tuple[float, float]


def load_reference(reference_dir: Path) -> ReferenceData:
    """Читает data/reference/norms.json и data/reference/geocode_fallback.json."""
    norms_raw = json.loads((reference_dir / "norms.json").read_text(encoding="utf-8"))
    norms = {
        bk_type: NormEntry(**entry)
        for bk_type, entry in norms_raw["by_bk_type"].items()
    }

    fallback_raw = json.loads(
        (reference_dir / "geocode_fallback.json").read_text(encoding="utf-8")
    )
    fallback_default = tuple(fallback_raw["_default"])
    fallback_centroids = {
        district: tuple(coords)
        for district, coords in fallback_raw.items()
        if not district.startswith("_")
    }

    return ReferenceData(
        norms=norms,
        fallback_centroids=fallback_centroids,
        fallback_default=fallback_default,
    )


@dataclass(frozen=True)
class RawOrder:
    order_id: str
    bk_type: str
    hd_type: str
    start_dt: datetime
    end_dt: datetime
    district: str
    address: str
    connection_type: str | None
    gigabit: bool
    source_file: str


@dataclass(frozen=True)
class OfficeInfo:
    source_file: str
    address: str


def _parse_bool_da_net(value: str | None) -> bool:
    return (value or "").strip().lower() == "да"


def load_synthetic_orders(raw_dir: Path) -> tuple[list[RawOrder], list[OfficeInfo]]:
    """Читает все data/raw/*.csv (схема без Статус BK / Бригада).

    Хвостовая строка "Адрес Офиса" — не заявка, а стартовая точка инженеров
    региона; извлекается отдельно в OfficeInfo.
    """
    orders: list[RawOrder] = []
    offices: list[OfficeInfo] = []

    for csv_path in sorted(raw_dir.glob("*.csv")):
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                order_id_raw = (row.get("Заявка") or "").strip()

                if order_id_raw == "Адрес Офиса":
                    # Эта строка — не заявка, а метаданные, экспортированные по
                    # той же позиции колонок: адрес лежит во втором столбце
                    # ("Тип заявки BK" — неверное имя для этой строки, но это
                    # тот же физический столбец CSV).
                    offices.append(
                        OfficeInfo(
                            source_file=csv_path.name,
                            address=(row.get("Тип заявки BK") or "").strip(),
                        )
                    )
                    continue

                if not order_id_raw.isdigit():
                    logger.warning(
                        "Пропущена нераспознанная строка в %s: %r",
                        csv_path.name,
                        row,
                    )
                    continue

                orders.append(
                    RawOrder(
                        order_id=order_id_raw,
                        bk_type=(row.get("Тип заявки BK") or "").strip(),
                        hd_type=(row.get("Тип заявки HD") or "").strip(),
                        start_dt=datetime.strptime(row["Начало"].strip(), DATETIME_FORMAT),
                        end_dt=datetime.strptime(row["Окончание"].strip(), DATETIME_FORMAT),
                        district=(row.get("Район") or "").strip(),
                        address=(row.get("Адрес") or "").strip(),
                        connection_type=(row.get("Подключение") or "").strip() or None,
                        gigabit=_parse_bool_da_net(row.get("Гигабитное подключение")),
                        source_file=csv_path.name,
                    )
                )

    return orders, offices


@dataclass(frozen=True)
class HistoricalOrder:
    order_id: str
    bk_type: str
    status: str
    hd_type: str
    start_dt: datetime
    end_dt: datetime
    district: str
    address: str
    brigade: str
    gigabit: bool
    source_file: str


def load_historical_control(historical_dir: Path) -> list[HistoricalOrder]:
    """Читает все data/reference/historical/*.csv (схема со Статус BK/Бригада).

    Все статусы сохраняются как есть — "Отменена"/"Выполнена" не фильтруются,
    это реальная история дня, а не мусор для отбрасывания.
    """
    orders: list[HistoricalOrder] = []

    for csv_path in sorted(historical_dir.glob("*.csv")):
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                order_id_raw = (row.get("Заявка") or "").strip()

                if not order_id_raw.isdigit():
                    logger.warning(
                        "Пропущена нераспознанная строка в %s: %r",
                        csv_path.name,
                        row,
                    )
                    continue

                orders.append(
                    HistoricalOrder(
                        order_id=order_id_raw,
                        bk_type=(row.get("Тип заявки BK") or "").strip(),
                        status=(row.get("Статус BK") or "").strip(),
                        hd_type=(row.get("Тип заявки HD") or "").strip(),
                        start_dt=datetime.strptime(row["Начало"].strip(), DATETIME_FORMAT),
                        end_dt=datetime.strptime(row["Окончание"].strip(), DATETIME_FORMAT),
                        district=(row.get("Район") or "").strip(),
                        address=(row.get("Адрес") or "").strip(),
                        brigade=(row.get("Бригада") or "").strip(),
                        gigabit=_parse_bool_da_net(row.get("Гигабитное подключение")),
                        source_file=csv_path.name,
                    )
                )

    return orders
