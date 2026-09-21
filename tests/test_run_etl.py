import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from etl.parse_raw import NormEntry, RawOrder, ReferenceData
from etl.run_etl import _parse_args, build_order_record, main, run
from geocode import GeocodeResult


class FakeGeocoder:
    def __init__(self, result: GeocodeResult) -> None:
        self._result = result
        self.calls: list[tuple[str, str | None]] = []

    def geocode_address(self, address: str, district: str | None = None) -> GeocodeResult:
        self.calls.append((address, district))
        return self._result


def _reference() -> ReferenceData:
    return ReferenceData(
        norms={
            "Подключение": NormEntry(
                road_min=20, tech_min=60, docs_min=10, base_norm_min=90,
                duration_on_site_min=70, skill="connect_order",
                skill_name_ru="Работы на подключение и дозаказы",
                priority="Обычная",
            )
        },
        fallback_centroids={},
        fallback_default=(55.7558, 37.6173),
    )


def test_build_order_record_maps_norms_and_geocode():
    raw = RawOrder(
        order_id="74198",
        bk_type="Подключение",
        hd_type="Конвергенция абонента",
        start_dt=datetime(2026, 8, 17, 20, 0),
        end_dt=datetime(2026, 8, 17, 22, 0),
        district="Кузьминки",
        address="Город Москва, пр-кт.Волгоградский, д. 128 к 5",
        connection_type="FMC",
        gigabit=False,
        source_file="vostok_synthetic.csv",
    )
    geocoder = FakeGeocoder(GeocodeResult(lat=55.705, lon=37.755, source="nominatim"))

    record = build_order_record(raw, geocoder, _reference())

    assert record["id"] == "74198"
    assert record["lat"] == 55.705
    assert record["lon"] == 37.755
    assert record["geocode_source"] == "nominatim"
    assert record["duration_min"] == 70
    assert record["window_start"] == "20:00"
    assert record["window_end"] == "22:00"
    assert record["date"] == "2026-08-17"
    assert record["priority"] == "Обычная"
    assert record["required_skill"] == "Работы на подключение и дозаказы"
    assert record["required_transport"] is None
    assert record["source_file"] == "vostok_synthetic.csv"
    assert geocoder.calls == [("Город Москва, пр-кт.Волгоградский, д. 128 к 5", "Кузьминки")]


def test_build_order_record_unknown_bk_type_falls_back_to_defaults():
    raw = RawOrder(
        order_id="1",
        bk_type="Незнакомый тип",
        hd_type="",
        start_dt=datetime(2026, 8, 17, 10, 0),
        end_dt=datetime(2026, 8, 17, 12, 0),
        district="Кузьминки",
        address="Адрес",
        connection_type=None,
        gigabit=False,
        source_file="vostok_synthetic.csv",
    )
    geocoder = FakeGeocoder(GeocodeResult(lat=1.0, lon=2.0, source="nominatim"))

    record = build_order_record(raw, geocoder, _reference())

    assert record["duration_min"] is None
    assert record["priority"] == "Обычная"
    assert record["required_skill"] is None


RAW_CSV = (
    "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;"
    "Подключение;Гигабитное подключение\n"
    "74198;Подключение;Конвергенция абонента;17.08.2026 20:00;17.08.2026 22:00;"
    "Кузьминки;Город Москва, пр-кт.Волгоградский, д. 128 к 5;FMC;Нет\n"
    "Адрес Офиса;г. Москва, ул Юных Ленинцев, д 83с 4;;;;;;;\n"
)

HISTORICAL_CSV = (
    "Заявка;Тип заявки BK;Статус BK;Тип заявки HD;Начало;Окончание;Район;"
    "Адрес;Бригада;Гигабитное подключение\n"
    "305885689;Подключение;Отменена;Конвергенция абонента;"
    "17.08.2026 12:00;17.08.2026 14:00;Зюзино;"
    "Город Москва, ул.Херсонская, д. 18, кв. 66;Прокопенко Вячеслав;Нет\n"
)

NORMS_JSON = json.dumps(
    {
        "by_bk_type": {
            "Подключение": {
                "road_min": 20, "tech_min": 60, "docs_min": 10,
                "base_norm_min": 90, "duration_on_site_min": 70,
                "skill": "connect_order",
                "skill_name_ru": "Работы на подключение и дозаказы",
                "priority": "Обычная",
            }
        }
    },
    ensure_ascii=False,
)

FALLBACK_JSON = json.dumps(
    {"_default": [55.7558, 37.6173], "Кузьминки": [55.705, 37.755], "Зюзино": [55.66, 37.59]},
    ensure_ascii=False,
)


def _build_project(tmp_path):
    raw_dir = tmp_path / "data" / "raw"
    reference_dir = tmp_path / "data" / "reference"
    historical_dir = reference_dir / "historical"
    output_dir = tmp_path / "data" / "output"
    raw_dir.mkdir(parents=True)
    historical_dir.mkdir(parents=True)

    (raw_dir / "vostok_synthetic.csv").write_text(RAW_CSV, encoding="utf-8")
    (historical_dir / "control_set_a.csv").write_text(HISTORICAL_CSV, encoding="utf-8")
    (reference_dir / "norms.json").write_text(NORMS_JSON, encoding="utf-8")
    (reference_dir / "geocode_fallback.json").write_text(FALLBACK_JSON, encoding="utf-8")

    return raw_dir, reference_dir, output_dir


def test_run_writes_all_output_files(tmp_path):
    raw_dir, reference_dir, output_dir = _build_project(tmp_path)

    summary = run(
        raw_dir=raw_dir,
        reference_dir=reference_dir,
        output_dir=output_dir,
        no_network=True,
        limit=None,
    )

    assert summary["synthetic_orders"] == 1
    assert summary["historical_orders"] == 1
    assert summary["offices"] == 1

    orders = json.loads((output_dir / "orders.json").read_text(encoding="utf-8"))
    assert orders[0]["id"] == "74198"
    assert orders[0]["geocode_source"] == "fallback:Кузьминки"

    assert (output_dir / "orders.csv").exists()

    offices = json.loads((output_dir / "offices.json").read_text(encoding="utf-8"))
    assert offices[0]["address"] == "г. Москва, ул Юных Ленинцев, д 83с 4"

    historical = json.loads((output_dir / "historical_reference.json").read_text(encoding="utf-8"))
    assert historical[0]["status"] == "Отменена"
    assert historical[0]["historical_brigade"] == "Прокопенко Вячеслав"


def test_run_limit_truncates_synthetic_orders(tmp_path):
    raw_dir, reference_dir, output_dir = _build_project(tmp_path)

    summary = run(
        raw_dir=raw_dir,
        reference_dir=reference_dir,
        output_dir=output_dir,
        no_network=True,
        limit=0,
    )

    assert summary["synthetic_orders"] == 0


def test_run_counts_office_geocode_source_in_summary(tmp_path):
    raw_dir, reference_dir, output_dir = _build_project(tmp_path)

    summary = run(
        raw_dir=raw_dir,
        reference_dir=reference_dir,
        output_dir=output_dir,
        no_network=True,
        limit=None,
    )

    # 1 заявка + 1 историческая запись + 1 офис — все ушли в fallback (сеть
    # выключена), офис должен учитываться наравне с заявками, а не молча
    # выпадать из сводки.
    assert summary["geocoded_fallback"] == 3


def test_run_raises_clear_error_when_raw_dir_is_missing(tmp_path):
    _, reference_dir, output_dir = _build_project(tmp_path)
    missing_raw_dir = tmp_path / "does_not_exist"

    with pytest.raises(FileNotFoundError, match="Директория с сырыми заявками"):
        run(
            raw_dir=missing_raw_dir,
            reference_dir=reference_dir,
            output_dir=output_dir,
            no_network=True,
            limit=None,
        )


def test_run_raises_clear_error_when_historical_dir_is_missing(tmp_path):
    raw_dir, reference_dir, output_dir = _build_project(tmp_path)
    shutil.rmtree(reference_dir / "historical")

    with pytest.raises(FileNotFoundError, match="исторически"):
        run(
            raw_dir=raw_dir,
            reference_dir=reference_dir,
            output_dir=output_dir,
            no_network=True,
            limit=None,
        )


def test_parse_args_defaults_and_flags():
    args = _parse_args(["--no-network", "--limit", "3"])

    assert args.raw_dir == Path("data/raw")
    assert args.reference_dir == Path("data/reference")
    assert args.output_dir == Path("data/output")
    assert args.no_network is True
    assert args.limit == 3


def test_parse_args_no_network_defaults_to_false():
    args = _parse_args([])

    assert args.no_network is False
    assert args.limit is None


def test_main_prints_clean_error_and_exits_nonzero_for_missing_dir(tmp_path, capsys):
    _, reference_dir, output_dir = _build_project(tmp_path)
    missing_raw_dir = tmp_path / "does_not_exist"

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--raw-dir", str(missing_raw_dir),
                "--reference-dir", str(reference_dir),
                "--output-dir", str(output_dir),
                "--no-network",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err
    assert "Traceback" not in captured.err
