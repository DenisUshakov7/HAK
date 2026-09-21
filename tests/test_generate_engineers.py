import json
from pathlib import Path

import pytest

from engineers.generate_engineers import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    SHIFT_DURATION_WEIGHTS,
    SHIFT_DURATIONS,
    SKILLS,
    VEHICLE_WEIGHTS,
    VEHICLES,
    Engineer,
    _format_hh_mm,
    _load_brigade_name_pool,
    _load_office_coords,
    _parse_args,
    _pick_skills,
    generate_engineers,
    main,
    run,
)


def test_skills_vehicles_reference_lists_match_spec():
    assert SKILLS == [
        "Локальные работы",
        "Работы на подключение и дозаказы",
        "Аварийные работы",
    ]
    assert VEHICLES == ["Автомобиль", "Пешеход", "Велосипед", "Общественный транспорт"]
    assert len(VEHICLE_WEIGHTS) == len(VEHICLES)
    assert len(SHIFT_DURATION_WEIGHTS) == len(SHIFT_DURATIONS)


def test_format_hh_mm():
    assert _format_hh_mm(8) == "08:00"
    assert _format_hh_mm(23) == "23:00"


def test_pick_skills_one_skill_cycles_through_all_three():
    # 4 инженера с 1 навыком, group_index 0..3 — должны покрыть все 3
    # навыка, а не всегда один и тот же.
    picked = [_pick_skills(group_index=i, skill_count=1) for i in range(4)]
    assert set(SKILLS) <= {skill for combo in picked for skill in combo}
    assert all(len(combo) == 1 for combo in picked)


def test_pick_skills_three_skills_returns_all():
    assert _pick_skills(group_index=0, skill_count=3) == SKILLS


def test_engineer_is_a_plain_dataclass_with_expected_fields():
    eng = Engineer(
        id="eng_01",
        name="Тест Тестов",
        start_lat=55.7,
        start_lon=37.6,
        shift_start="10:00",
        shift_end="18:00",
        skills=["Локальные работы"],
        vehicle="Пешеход",
    )
    assert eng.id == "eng_01"
    assert eng.skills == ["Локальные работы"]


HISTORICAL_HEADER = (
    "Заявка;Тип заявки BK;Статус BK;Тип заявки HD;Начало;Окончание;Район;"
    "Адрес;Бригада;Гигабитное подключение\n"
)


def _write_historical_csv(tmp_path, filename: str, brigades: list[str]):
    historical_dir = tmp_path / "historical"
    historical_dir.mkdir(exist_ok=True)
    rows = "".join(
        f"{100000 + i};Подключение;Отправлена;Конвергенция абонента;"
        f"17.08.2026 10:00;17.08.2026 12:00;Кузьминки;"
        f"Город Москва, ул.Тестовая, д. {i};{brigade};Нет\n"
        for i, brigade in enumerate(brigades)
    )
    (historical_dir / filename).write_text(HISTORICAL_HEADER + rows, encoding="utf-8")
    return historical_dir


def test_load_brigade_name_pool_returns_unique_sorted_names(tmp_path):
    historical_dir = _write_historical_csv(
        tmp_path, "control_set_a.csv", ["Иванов Иван", "Петров Пётр", "Иванов Иван"]
    )

    pool = _load_brigade_name_pool(historical_dir)

    assert pool == ["Иванов Иван", "Петров Пётр"]


def test_load_brigade_name_pool_combines_multiple_files(tmp_path):
    historical_dir = tmp_path / "historical"
    historical_dir.mkdir()
    _write_historical_csv(tmp_path, "control_set_a.csv", ["Иванов Иван"])
    _write_historical_csv(tmp_path, "control_set_b.csv", ["Петров Пётр"])

    pool = _load_brigade_name_pool(historical_dir)

    assert pool == ["Иванов Иван", "Петров Пётр"]


def test_load_office_coords_reads_first_entry(tmp_path):
    offices_path = tmp_path / "offices.json"
    offices_path.write_text(
        json.dumps(
            [{"source_file": "x.csv", "address": "Тест", "lat": 55.7, "lon": 37.6, "geocode_source": "nominatim"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    lat, lon = _load_office_coords(offices_path)

    assert lat == 55.7
    assert lon == 37.6


def test_load_office_coords_raises_on_empty_list(tmp_path):
    offices_path = tmp_path / "offices.json"
    offices_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="офис"):
        _load_office_coords(offices_path)


NAME_POOL = [f"Инженер {i}" for i in range(23)]


def test_generate_engineers_is_deterministic_with_same_seed():
    first = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, seed=42)
    second = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, seed=42)

    assert first == second


def test_generate_engineers_different_seed_gives_different_order():
    first = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, seed=1)
    second = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, seed=2)

    assert [e.name for e in first] != [e.name for e in second]


def test_generate_engineers_returns_requested_count_with_unique_names():
    engineers = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, count=12, seed=7)

    assert len(engineers) == 12
    assert len({e.name for e in engineers}) == 12
    assert len({e.id for e in engineers}) == 12


def test_generate_engineers_skill_group_sizes_and_coverage():
    engineers = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, count=12, seed=7)

    counts = sorted(len(e.skills) for e in engineers)
    assert counts == [1] * 4 + [2] * 4 + [3] * 4

    all_skills_seen = {skill for e in engineers for skill in e.skills}
    assert all_skills_seen == set(SKILLS)


def test_generate_engineers_covers_all_vehicle_types():
    engineers = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, count=12, seed=7)

    vehicles_seen = {e.vehicle for e in engineers}
    assert vehicles_seen == set(VEHICLES)


def test_generate_engineers_shift_bounds_and_duration():
    engineers = generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, count=12, seed=7)

    for e in engineers:
        start_hour = int(e.shift_start.split(":")[0])
        end_hour = int(e.shift_end.split(":")[0])
        duration = end_hour - start_hour

        assert 8 <= start_hour <= 20
        assert end_hour <= 23
        assert duration in SHIFT_DURATIONS


def test_generate_engineers_start_point_matches_office():
    engineers = generate_engineers(NAME_POOL, office_lat=55.71, office_lon=37.61, count=12, seed=7)

    assert all(e.start_lat == 55.71 and e.start_lon == 37.61 for e in engineers)


def test_generate_engineers_raises_if_pool_too_small():
    with pytest.raises(ValueError, match="пул"):
        generate_engineers(["Один", "Два"], office_lat=55.7, office_lon=37.6, count=12, seed=7)


def test_generate_engineers_raises_if_count_not_divisible_by_three():
    with pytest.raises(ValueError, match="делиться на 3"):
        generate_engineers(NAME_POOL, office_lat=55.7, office_lon=37.6, count=10, seed=7)


OFFICES_JSON = json.dumps(
    [{"source_file": "vostok_synthetic.csv", "address": "Тест", "lat": 55.7, "lon": 37.6, "geocode_source": "nominatim"}],
    ensure_ascii=False,
)


def test_run_writes_engineers_json(tmp_path):
    historical_dir = _write_historical_csv(tmp_path, "control_set_a.csv", NAME_POOL)
    offices_path = tmp_path / "offices.json"
    offices_path.write_text(OFFICES_JSON, encoding="utf-8")
    output_path = tmp_path / "output" / "engineers.json"

    engineers = run(
        historical_dir=historical_dir,
        offices_path=offices_path,
        output_path=output_path,
        count=12,
        seed=7,
    )

    assert len(engineers) == 12
    assert output_path.exists()

    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(written) == 12
    assert written[0]["start_lat"] == 55.7
    assert written[0]["start_lon"] == 37.6
    assert "skills" in written[0]
    assert "vehicle" in written[0]


def test_run_raises_clear_error_when_historical_dir_is_missing(tmp_path):
    offices_path = tmp_path / "offices.json"
    offices_path.write_text(OFFICES_JSON, encoding="utf-8")
    missing_historical_dir = tmp_path / "does_not_exist"

    with pytest.raises(FileNotFoundError, match="исторически"):
        run(
            historical_dir=missing_historical_dir,
            offices_path=offices_path,
            output_path=tmp_path / "output" / "engineers.json",
            count=12,
            seed=7,
        )


def test_parse_args_defaults_and_flags():
    args = _parse_args(["--count", "6", "--seed", "1"])

    assert args.historical_dir == Path("data/reference/historical")
    assert args.offices_path == Path("data/output/offices.json")
    assert args.output == Path("data/output/engineers.json")
    assert args.count == 6
    assert args.seed == 1


def test_parse_args_uses_module_defaults_when_omitted():
    args = _parse_args([])

    assert args.count == DEFAULT_COUNT
    assert args.seed == DEFAULT_SEED


def test_main_prints_clean_error_and_exits_nonzero_for_missing_dir(tmp_path, capsys):
    offices_path = tmp_path / "offices.json"
    offices_path.write_text(OFFICES_JSON, encoding="utf-8")
    missing_historical_dir = tmp_path / "does_not_exist"

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--historical-dir", str(missing_historical_dir),
                "--offices-path", str(offices_path),
                "--output", str(tmp_path / "output" / "engineers.json"),
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "Traceback" not in captured.err
