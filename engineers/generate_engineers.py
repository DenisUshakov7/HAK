"""Генерация синтетических инженеров для региона.

Реальные имена бригад берутся из исторических данных
(data/reference/historical/*.csv) — навыки, смена и транспорт полностью
синтетические, т.к. в исходной выгрузке этих данных нет вообще.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS = [
    "Локальные работы",
    "Работы на подключение и дозаказы",
    "Аварийные работы",
]
VEHICLES = ["Автомобиль", "Пешеход", "Велосипед", "Общественный транспорт"]
# Порядок весов соответствует порядку VEHICLES. Пешеход/общественный
# транспорт — большинство (по словам заказчика на Q&A-сессии), авто и
# велосипед — меньшинство.
VEHICLE_WEIGHTS = [0.20, 0.35, 0.10, 0.35]

SHIFT_DURATIONS = [4, 5, 6, 7, 8, 9]
# 8 и 9 часов (полная смена) — большинство (~60% суммарно), 6-7 часов —
# ~25%, 4-5 часов (неполная занятость) — меньшинство ~15%.
SHIFT_DURATION_WEIGHTS = [0.075, 0.075, 0.125, 0.125, 0.30, 0.30]

DEFAULT_SEED = 20260817
DEFAULT_COUNT = 12


@dataclass(frozen=True)
class Engineer:
    id: str
    name: str
    start_lat: float
    start_lon: float
    shift_start: str
    shift_end: str
    skills: list[str]
    vehicle: str


def _format_hh_mm(hour: int) -> str:
    return f"{hour:02d}:00"


def _pick_skills(group_index: int, skill_count: int) -> list[str]:
    """Комбинация навыков для инженера с данным skill_count.

    group_index — порядковый номер инженера внутри своей группы (0, 1, 2, ...).
    Комбинации навыков чередуются по кругу (group_index % число комбинаций),
    чтобы не все инженеры с одинаковым skill_count получали один и тот же
    набор — иначе квалификационное ограничение выродится в тривиальное.
    """
    combos = list(combinations(SKILLS, skill_count))
    return list(combos[group_index % len(combos)])


def _load_brigade_name_pool(historical_dir: Path) -> list[str]:
    """Уникальные имена бригад из всех data/reference/historical/*.csv."""
    from etl.parse_raw import load_historical_control

    orders = load_historical_control(historical_dir)
    return sorted({order.brigade for order in orders if order.brigade})


def _load_office_coords(offices_path: Path) -> tuple[float, float]:
    """Координаты первого (и обычно единственного) офиса в offices.json."""
    offices = json.loads(offices_path.read_text(encoding="utf-8"))
    if not offices:
        raise ValueError(f"В {offices_path} нет ни одного офиса")
    first = offices[0]
    return first["lat"], first["lon"]


def generate_engineers(
    name_pool: list[str],
    office_lat: float,
    office_lon: float,
    count: int = DEFAULT_COUNT,
    seed: int = DEFAULT_SEED,
) -> list[Engineer]:
    """Генерирует `count` инженеров: реальные имена из name_pool (без
    повторов), координаты старта — office_lat/office_lon, синтетические
    навыки/смена/транспорт со взвешенным распределением.

    Детерминировано относительно seed — одинаковый seed даёт одинаковый
    результат при одном и том же name_pool.
    """
    if len(name_pool) < count:
        raise ValueError(
            f"В пуле имён {len(name_pool)} записей, нужно как минимум {count}"
        )
    if count % 3 != 0:
        raise ValueError(f"count={count} должен делиться на 3 (группы по 1/2/3 навыка)")

    rng = random.Random(seed)
    names = rng.sample(name_pool, count)

    per_group = count // 3
    skill_counts = [1] * per_group + [2] * per_group + [3] * per_group

    # Первые len(VEHICLES) инженеров получают каждый свой тип транспорта
    # (чтобы были представлены все), остальные — по весам; затем порядок
    # перемешивается.
    vehicles = list(VEHICLES)
    rng.shuffle(vehicles)
    vehicles += rng.choices(VEHICLES, weights=VEHICLE_WEIGHTS, k=count - len(VEHICLES))
    rng.shuffle(vehicles)

    engineers: list[Engineer] = []
    group_counters = {1: 0, 2: 0, 3: 0}
    for i in range(count):
        skill_count = skill_counts[i]
        skills = _pick_skills(group_counters[skill_count], skill_count)
        group_counters[skill_count] += 1

        duration = rng.choices(SHIFT_DURATIONS, weights=SHIFT_DURATION_WEIGHTS, k=1)[0]
        max_start = min(20, 23 - duration)
        start_hour = rng.randint(8, max_start)

        engineers.append(
            Engineer(
                id=f"eng_{i + 1:02d}",
                name=names[i],
                start_lat=office_lat,
                start_lon=office_lon,
                shift_start=_format_hh_mm(start_hour),
                shift_end=_format_hh_mm(start_hour + duration),
                skills=skills,
                vehicle=vehicles[i],
            )
        )

    return engineers


def _write_engineers(engineers: list[Engineer], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(e) for e in engineers], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run(
    historical_dir: Path,
    offices_path: Path,
    output_path: Path,
    count: int = DEFAULT_COUNT,
    seed: int = DEFAULT_SEED,
) -> list[Engineer]:
    # glob() на несуществующей директории вернёт пустой список без ошибки —
    # проверяем путь явно, чтобы сообщение было понятным.
    if not historical_dir.is_dir():
        raise FileNotFoundError(
            f"Директория с историческими данными не найдена: {historical_dir}"
        )

    name_pool = _load_brigade_name_pool(historical_dir)
    office_lat, office_lon = _load_office_coords(offices_path)
    engineers = generate_engineers(
        name_pool, office_lat, office_lon, count=count, seed=seed
    )
    _write_engineers(engineers, output_path)
    return engineers


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сгенерировать синтетических инженеров для региона."
    )
    parser.add_argument(
        "--historical-dir", type=Path, default=Path("data/reference/historical")
    )
    parser.add_argument(
        "--offices-path", type=Path, default=Path("data/output/offices.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/output/engineers.json")
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        # Ожидаемые ошибки (нет файла, некорректные параметры) ловим явно,
        # а не bare except.
        engineers = run(
            historical_dir=args.historical_dir,
            offices_path=args.offices_path,
            output_path=args.output,
            count=args.count,
            seed=args.seed,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        json.dumps(
            {"generated": len(engineers), "output": str(args.output)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
