import json
from pathlib import Path

import pytest

from routemap.build_map import _build_legend, _parse_args, main, run
from routing.models import Engineer

OFFICE = (55.70, 37.60)


def _engineer(id_, name, vehicle="Пешеход"):
    return Engineer(id_, name, *OFFICE, "08:00", "20:00", ["Локальные работы"], vehicle)


def test_build_legend_includes_route_stops_and_reasons():
    assignment = {
        "routes": [
            {
                "engineer_id": "eng_01",
                "engineer_name": "Тест Тестов",
                "stops": [
                    {
                        "order_id": "1",
                        "arrival": "10:00",
                        "travel_min": 5,
                        "distance_km": 1.2,
                        "reason": "Причина назначения",
                    }
                ],
                "total_distance_km": 1.2,
            }
        ],
        "unassigned": [{"order_id": "2", "reason": "Причина отказа"}],
        "metrics": {
            "engineers_used": 1,
            "total_distance_km": 1.2,
            "orders_assigned": 1,
            "orders_unassigned": 1,
        },
    }
    engineers = [_engineer("eng_01", "Тест Тестов")]

    legend = _build_legend(assignment, engineers)

    assert "Тест Тестов" in legend
    assert "Причина назначения" in legend
    assert "Причина отказа" in legend
    assert "10:00" in legend


def test_build_legend_lists_idle_engineers():
    assignment = {
        "routes": [],
        "unassigned": [],
        "metrics": {
            "engineers_used": 0,
            "total_distance_km": 0.0,
            "orders_assigned": 0,
            "orders_unassigned": 0,
        },
    }
    engineers = [_engineer("eng_01", "Простаивающий")]

    legend = _build_legend(assignment, engineers)

    assert "Простаивающий" in legend
    assert "Инженеры без назначений" in legend


def test_build_legend_escapes_html_in_reason():
    assignment = {
        "routes": [],
        "unassigned": [{"order_id": "1", "reason": "<script>alert(1)</script>"}],
        "metrics": {
            "engineers_used": 0,
            "total_distance_km": 0.0,
            "orders_assigned": 0,
            "orders_unassigned": 1,
        },
    }

    legend = _build_legend(assignment, [])

    assert "<script>" not in legend
    assert "&lt;script&gt;" in legend


def test_build_legend_includes_comparison_when_provided():
    assignment = {
        "routes": [],
        "unassigned": [],
        "metrics": {
            "engineers_used": 0,
            "total_distance_km": 0.0,
            "orders_assigned": 0,
            "orders_unassigned": 0,
        },
    }
    comparison = {
        "engineers_used": {"optimized": 11, "baseline": 14},
        "total_distance_km": {"optimized": 195.21, "baseline": 260.87},
        "orders_assigned": {"optimized": 56, "baseline": 50},
        "orders_unassigned": {"optimized": 10, "baseline": 16},
    }

    legend = _build_legend(assignment, [], comparison)

    assert "Сравнение с базовым вариантом" in legend
    assert "260.87" in legend
    assert "195.21" in legend


def test_build_legend_omits_comparison_when_none():
    assignment = {
        "routes": [],
        "unassigned": [],
        "metrics": {
            "engineers_used": 0,
            "total_distance_km": 0.0,
            "orders_assigned": 0,
            "orders_unassigned": 0,
        },
    }

    legend = _build_legend(assignment, [])

    assert "Сравнение с базовым вариантом" not in legend


OFFICE_RECORD = {
    "source_file": "x.csv",
    "address": "тест",
    "lat": 55.70,
    "lon": 37.60,
    "geocode_source": "nominatim",
}


def _order_record(id_, lat, lon):
    return {
        "id": id_,
        "address": "тест",
        "district": "тест",
        "lat": lat,
        "lon": lon,
        "geocode_source": "nominatim",
        "duration_min": 10,
        "window_start": "09:00",
        "window_end": "18:00",
        "date": "2026-08-17",
        "priority": "Обычная",
        "required_skill": "Локальные работы",
        "required_transport": None,
        "gigabit": False,
        "source_file": "x.csv",
    }


def _engineer_record(id_, name, vehicle="Пешеход"):
    return {
        "id": id_,
        "name": name,
        "start_lat": OFFICE_RECORD["lat"],
        "start_lon": OFFICE_RECORD["lon"],
        "shift_start": "08:00",
        "shift_end": "20:00",
        "skills": ["Локальные работы"],
        "vehicle": vehicle,
    }


def _write_common_inputs(tmp_path, engineer_ids_names):
    orders_path = tmp_path / "orders.json"
    engineers_path = tmp_path / "engineers.json"
    offices_path = tmp_path / "offices.json"
    orders_path.write_text(
        json.dumps(
            [_order_record("1", 55.701, 37.601), _order_record("2", 55.702, 37.602)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engineers_path.write_text(
        json.dumps(
            [_engineer_record(id_, name) for id_, name in engineer_ids_names],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    offices_path.write_text(json.dumps([OFFICE_RECORD], ensure_ascii=False), encoding="utf-8")
    return orders_path, engineers_path, offices_path


def test_run_creates_html_with_route_and_unassigned_markers(tmp_path):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов"), ("eng_02", "Простаивающий")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "engineer_id": "eng_01",
                        "engineer_name": "Тест Тестов",
                        "stops": [
                            {
                                "order_id": "1",
                                "arrival": "10:00",
                                "travel_min": 5,
                                "distance_km": 1.2,
                                "reason": "Причина назначения",
                            }
                        ],
                        "total_distance_km": 1.2,
                    }
                ],
                "unassigned": [{"order_id": "2", "reason": "Причина отказа"}],
                "metrics": {
                    "engineers_used": 1,
                    "total_distance_km": 1.2,
                    "orders_assigned": 1,
                    "orders_unassigned": 1,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        cache_path=tmp_path / "cache.json",
        force_fallback=True,
        comparison_path=tmp_path / "no_comparison.json",
    )

    assert result == output_path
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Тест Тестов" in content
    assert "Причина назначения" in content
    assert "Причина отказа" in content
    assert "Простаивающий" in content
    assert "Офис" in content


def test_run_draws_geometry_coordinates_when_available(tmp_path, monkeypatch):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "engineer_id": "eng_01",
                        "engineer_name": "Тест Тестов",
                        "stops": [
                            {
                                "order_id": "1",
                                "arrival": "10:00",
                                "travel_min": 5,
                                "distance_km": 1.2,
                                "reason": "Причина",
                            }
                        ],
                        "total_distance_km": 1.2,
                    }
                ],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 1,
                    "total_distance_km": 1.2,
                    "orders_assigned": 1,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    monkeypatch.setattr(
        "routemap.build_map.DistanceMatrixBuilder.fetch_geometry",
        lambda self, profile, points: [(55.7001, 37.6001), (55.70015, 37.60015), (55.701, 37.601)],
    )

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        cache_path=tmp_path / "cache.json",
        comparison_path=tmp_path / "no_comparison.json",
    )

    content = result.read_text(encoding="utf-8")
    assert "55.70015" in content


def test_run_falls_back_to_straight_line_without_network(tmp_path, monkeypatch):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "engineer_id": "eng_01",
                        "engineer_name": "Тест Тестов",
                        "stops": [
                            {
                                "order_id": "1",
                                "arrival": "10:00",
                                "travel_min": 5,
                                "distance_km": 1.2,
                                "reason": "Причина",
                            }
                        ],
                        "total_distance_km": 1.2,
                    }
                ],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 1,
                    "total_distance_km": 1.2,
                    "orders_assigned": 1,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    def fake_post(url, json, headers, timeout):
        raise AssertionError("не должно быть сетевых запросов при force_fallback=True")

    monkeypatch.setattr("requests.post", fake_post)

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        cache_path=tmp_path / "cache.json",
        force_fallback=True,
        comparison_path=tmp_path / "no_comparison.json",
    )

    content = result.read_text(encoding="utf-8")
    assert "Тест Тестов" in content


def test_run_handles_no_routes_without_crashing(tmp_path):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Без заявок")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 0,
                    "total_distance_km": 0.0,
                    "orders_assigned": 0,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        comparison_path=tmp_path / "no_comparison.json",
    )

    assert result.exists()


def test_run_raises_for_missing_assignment_file(tmp_path):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест")]
    )

    with pytest.raises(FileNotFoundError):
        run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            assignment_path=tmp_path / "does_not_exist.json",
            output_path=tmp_path / "route_map.html",
            comparison_path=tmp_path / "no_comparison.json",
        )


def test_run_raises_clear_error_for_stale_assignment(tmp_path):
    # assignment.json ссылается на заявку, которой больше нет в orders.json
    # (например, orders.json перегенерировали после сборки assignment.json).
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "engineer_id": "eng_01",
                        "engineer_name": "Тест Тестов",
                        "stops": [
                            {
                                "order_id": "999",
                                "arrival": "10:00",
                                "travel_min": 5,
                                "distance_km": 1.2,
                                "reason": "Причина",
                            }
                        ],
                        "total_distance_km": 1.2,
                    }
                ],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 1,
                    "total_distance_km": 1.2,
                    "orders_assigned": 1,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="999"):
        run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            assignment_path=assignment_path,
            output_path=tmp_path / "route_map.html",
            comparison_path=tmp_path / "no_comparison.json",
        )


def test_run_raises_clear_error_for_stale_engineer(tmp_path):
    # assignment.json ссылается на инженера, которого больше нет в engineers.json.
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "engineer_id": "eng_999",
                        "engineer_name": "Призрак",
                        "stops": [
                            {
                                "order_id": "1",
                                "arrival": "10:00",
                                "travel_min": 5,
                                "distance_km": 1.2,
                                "reason": "Причина",
                            }
                        ],
                        "total_distance_km": 1.2,
                    }
                ],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 1,
                    "total_distance_km": 1.2,
                    "orders_assigned": 1,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="eng_999"):
        run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            assignment_path=assignment_path,
            output_path=tmp_path / "route_map.html",
            comparison_path=tmp_path / "no_comparison.json",
        )


def test_parse_args_defaults():
    args = _parse_args([])
    assert args.orders_path == Path("data/output/orders.json")
    assert args.engineers_path == Path("data/output/engineers.json")
    assert args.offices_path == Path("data/output/offices.json")
    assert args.assignment_path == Path("data/output/assignment.json")
    assert args.output == Path("data/output/route_map.html")
    assert args.no_network is False
    assert args.comparison_path == Path("data/output/comparison.json")


def test_main_prints_clean_error_and_exits_nonzero_for_missing_file(tmp_path, capsys):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест")]
    )

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--orders-path", str(orders_path),
                "--engineers-path", str(engineers_path),
                "--offices-path", str(offices_path),
                "--assignment-path", str(tmp_path / "does_not_exist.json"),
                "--output", str(tmp_path / "route_map.html"),
                "--comparison-path", str(tmp_path / "no_comparison.json"),
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "Traceback" not in captured.err


def test_main_prints_success_message_for_valid_input(tmp_path, capsys):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 0,
                    "total_distance_km": 0.0,
                    "orders_assigned": 0,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    main(
        [
            "--orders-path", str(orders_path),
            "--engineers-path", str(engineers_path),
            "--offices-path", str(offices_path),
            "--assignment-path", str(assignment_path),
            "--output", str(output_path),
            "--comparison-path", str(tmp_path / "no_comparison.json"),
        ]
    )

    captured = capsys.readouterr()
    assert "Карта сохранена" in captured.out
    assert output_path.exists()


def test_run_includes_comparison_block_when_file_present(tmp_path):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 0,
                    "total_distance_km": 0.0,
                    "orders_assigned": 0,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text(
        json.dumps(
            {
                "optimized": {},
                "baseline": {},
                "comparison": {
                    "engineers_used": {"optimized": 11, "baseline": 14},
                    "total_distance_km": {"optimized": 195.21, "baseline": 260.87},
                    "orders_assigned": {"optimized": 56, "baseline": 50},
                    "orders_unassigned": {"optimized": 10, "baseline": 16},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        comparison_path=comparison_path,
    )

    content = result.read_text(encoding="utf-8")
    assert "Сравнение с базовым вариантом" in content
    assert "260.87" in content


def test_run_omits_comparison_block_when_file_absent(tmp_path):
    orders_path, engineers_path, offices_path = _write_common_inputs(
        tmp_path, [("eng_01", "Тест Тестов")]
    )
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "routes": [],
                "unassigned": [],
                "metrics": {
                    "engineers_used": 0,
                    "total_distance_km": 0.0,
                    "orders_assigned": 0,
                    "orders_unassigned": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "route_map.html"

    result = run(
        orders_path=orders_path,
        engineers_path=engineers_path,
        offices_path=offices_path,
        assignment_path=assignment_path,
        output_path=output_path,
        comparison_path=tmp_path / "does_not_exist_comparison.json",
    )

    content = result.read_text(encoding="utf-8")
    assert "Сравнение с базовым вариантом" not in content
