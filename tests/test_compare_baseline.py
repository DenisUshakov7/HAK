import json
from pathlib import Path

import pytest

from routing.compare_baseline import _parse_args, main, run

OFFICE = {"source_file": "x.csv", "address": "тест", "lat": 55.70, "lon": 37.60, "geocode_source": "nominatim"}


def _order(id_, lat, lon, skill, window_start="09:00", window_end="18:00", transport=None):
    return {
        "id": id_,
        "address": "тест",
        "district": "тест",
        "lat": lat,
        "lon": lon,
        "geocode_source": "nominatim",
        "duration_min": 10,
        "window_start": window_start,
        "window_end": window_end,
        "date": "2026-08-17",
        "priority": "Обычная",
        "required_skill": skill,
        "required_transport": transport,
        "gigabit": False,
        "source_file": "x.csv",
    }


def _engineer(id_, name, skills, vehicle="Пешеход", shift_start="08:00", shift_end="20:00"):
    return {
        "id": id_,
        "name": name,
        "start_lat": OFFICE["lat"],
        "start_lon": OFFICE["lon"],
        "shift_start": shift_start,
        "shift_end": shift_end,
        "skills": skills,
        "vehicle": vehicle,
    }


def _write_inputs(tmp_path, orders, engineers):
    orders_path = tmp_path / "orders.json"
    engineers_path = tmp_path / "engineers.json"
    offices_path = tmp_path / "offices.json"
    orders_path.write_text(json.dumps(orders, ensure_ascii=False), encoding="utf-8")
    engineers_path.write_text(json.dumps(engineers, ensure_ascii=False), encoding="utf-8")
    offices_path.write_text(json.dumps([OFFICE], ensure_ascii=False), encoding="utf-8")
    return orders_path, engineers_path, offices_path


def test_run_writes_comparison_with_optimized_and_baseline(tmp_path):
    orders_path, engineers_path, offices_path = _write_inputs(
        tmp_path,
        [_order("1", 55.701, 37.601, "Локальные работы")],
        [_engineer("eng_01", "Тест Тестов", ["Локальные работы"])],
    )
    output_path = tmp_path / "output" / "comparison.json"

    output = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        output_path=output_path,
        force_fallback=True,
        cache_path=tmp_path / "cache.json",
    )

    assert output_path.exists()
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == output
    for plan_key in ("optimized", "baseline"):
        assert output[plan_key]["metrics"]["orders_assigned"] == 1
        assert output[plan_key]["routes"][0]["engineer_id"] == "eng_01"
    comparison = output["comparison"]
    assert comparison["engineers_used"] == {"optimized": 1, "baseline": 1}
    assert comparison["orders_assigned"] == {"optimized": 1, "baseline": 1}
    assert comparison["orders_unassigned"] == {"optimized": 0, "baseline": 0}
    assert set(comparison["total_distance_km"]) == {"optimized", "baseline"}


def test_run_shows_optimized_and_baseline_plans_can_genuinely_differ(tmp_path):
    # Порядок в файле (дальняя, ближняя, средняя точки) неоптимален по
    # пробегу: baseline обязан ехать в этом порядке, а OR-Tools — найти
    # более короткий маршрут, поэтому total_distance_km должны различаться.
    orders_path, engineers_path, offices_path = _write_inputs(
        tmp_path,
        [
            _order("far", 55.71, 37.60, "Локальные работы"),
            _order("near", 55.701, 37.601, "Локальные работы"),
            _order("mid", 55.705, 37.60, "Локальные работы"),
        ],
        [_engineer("eng_01", "Тест Тестов", ["Локальные работы"])],
    )
    output_path = tmp_path / "comparison.json"

    output = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        output_path=output_path,
        force_fallback=True,
        cache_path=tmp_path / "cache.json",
        time_limit_seconds=3,
    )

    baseline_stops = output["baseline"]["routes"][0]["stops"]
    assert [s["order_id"] for s in baseline_stops] == ["far", "near", "mid"]

    comparison = output["comparison"]
    assert comparison["total_distance_km"]["optimized"] < comparison["total_distance_km"]["baseline"]


def test_run_raises_for_empty_orders(tmp_path):
    orders_path, engineers_path, offices_path = _write_inputs(
        tmp_path, [], [_engineer("eng_01", "Тест", ["Локальные работы"])]
    )

    with pytest.raises(ValueError, match="ни одной заявки"):
        run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            output_path=tmp_path / "comparison.json",
            force_fallback=True,
            cache_path=tmp_path / "cache.json",
        )


def test_run_raises_for_empty_engineers(tmp_path):
    orders_path, engineers_path, offices_path = _write_inputs(
        tmp_path, [_order("1", 55.701, 37.601, "Локальные работы")], []
    )

    with pytest.raises(ValueError, match="ни одного инженера"):
        run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            output_path=tmp_path / "comparison.json",
            force_fallback=True,
            cache_path=tmp_path / "cache.json",
        )


def test_parse_args_defaults():
    args = _parse_args([])
    assert args.orders_path == Path("data/output/orders.json")
    assert args.engineers_path == Path("data/output/engineers.json")
    assert args.offices_path == Path("data/output/offices.json")
    assert args.output == Path("data/output/comparison.json")
    assert args.no_network is False


def test_main_prints_comparison_table_and_writes_file(tmp_path, capsys):
    orders_path, engineers_path, offices_path = _write_inputs(
        tmp_path,
        [_order("1", 55.701, 37.601, "Локальные работы")],
        [_engineer("eng_01", "Тест Тестов", ["Локальные работы"])],
    )
    output_path = tmp_path / "comparison.json"

    main(
        [
            "--orders-path", str(orders_path),
            "--engineers-path", str(engineers_path),
            "--offices-path", str(offices_path),
            "--output", str(output_path),
            "--no-network",
        ]
    )

    captured = capsys.readouterr()
    assert "Сравнение с базовым вариантом" in captured.out
    assert "Задействовано инженеров" in captured.out
    assert output_path.exists()


def test_main_prints_clean_error_and_exits_nonzero_for_missing_file(tmp_path, capsys):
    _, engineers_path, offices_path = _write_inputs(
        tmp_path, [], [_engineer("eng_01", "Тест", ["Локальные работы"])]
    )

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--orders-path", str(tmp_path / "does_not_exist.json"),
                "--engineers-path", str(engineers_path),
                "--offices-path", str(offices_path),
                "--output", str(tmp_path / "comparison.json"),
                "--no-network",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "Traceback" not in captured.err
