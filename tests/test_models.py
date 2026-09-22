import json
from pathlib import Path

import pytest

from routing.models import (
    Engineer,
    Order,
    format_hhmm,
    load_engineers,
    load_office_coords,
    load_orders,
    parse_hhmm,
)


def test_parse_hhmm():
    assert parse_hhmm("08:00") == 480
    assert parse_hhmm("23:45") == 1425


def test_format_hhmm():
    assert format_hhmm(480) == "08:00"
    assert format_hhmm(1425) == "23:45"


ORDERS_JSON = json.dumps(
    [
        {
            "id": "1",
            "address": "тест",
            "district": "тест",
            "lat": 55.7,
            "lon": 37.6,
            "geocode_source": "nominatim",
            "duration_min": 50,
            "window_start": "10:00",
            "window_end": "12:00",
            "date": "2026-08-17",
            "priority": "Обычная",
            "required_skill": "Локальные работы",
            "required_transport": None,
            "gigabit": False,
            "source_file": "x.csv",
        }
    ],
    ensure_ascii=False,
)

ENGINEERS_JSON = json.dumps(
    [
        {
            "id": "eng_01",
            "name": "Тест Тестов",
            "start_lat": 55.7,
            "start_lon": 37.6,
            "shift_start": "08:00",
            "shift_end": "16:00",
            "skills": ["Локальные работы"],
            "vehicle": "Пешеход",
        }
    ],
    ensure_ascii=False,
)

OFFICES_JSON = json.dumps(
    [{"source_file": "x.csv", "address": "тест", "lat": 55.71, "lon": 37.61, "geocode_source": "nominatim"}],
    ensure_ascii=False,
)


def test_load_orders_maps_fields(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(ORDERS_JSON, encoding="utf-8")

    orders = load_orders(path)

    assert orders == [
        Order(
            id="1",
            lat=55.7,
            lon=37.6,
            duration_min=50,
            window_start="10:00",
            window_end="12:00",
            priority="Обычная",
            required_skill="Локальные работы",
            required_transport=None,
        )
    ]


def test_load_engineers_maps_fields(tmp_path):
    path = tmp_path / "engineers.json"
    path.write_text(ENGINEERS_JSON, encoding="utf-8")

    engineers = load_engineers(path)

    assert engineers == [
        Engineer(
            id="eng_01",
            name="Тест Тестов",
            start_lat=55.7,
            start_lon=37.6,
            shift_start="08:00",
            shift_end="16:00",
            skills=["Локальные работы"],
            vehicle="Пешеход",
        )
    ]


def test_load_office_coords_reads_first_entry(tmp_path):
    path = tmp_path / "offices.json"
    path.write_text(OFFICES_JSON, encoding="utf-8")

    lat, lon = load_office_coords(path)

    assert (lat, lon) == (55.71, 37.61)


def test_load_office_coords_raises_on_empty_list(tmp_path):
    path = tmp_path / "offices.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="офис"):
        load_office_coords(path)
