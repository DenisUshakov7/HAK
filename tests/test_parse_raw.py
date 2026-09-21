import json
from datetime import datetime
from pathlib import Path

from etl.parse_raw import load_historical_control, load_reference, load_synthetic_orders


def _write_reference(tmp_path: Path) -> Path:
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    (reference_dir / "norms.json").write_text(
        json.dumps(
            {
                "by_bk_type": {
                    "Подключение": {
                        "road_min": 20, "tech_min": 60, "docs_min": 10,
                        "base_norm_min": 90, "duration_on_site_min": 70,
                        "skill": "connect_order",
                        "skill_name_ru": "Работы на подключение и дозаказы",
                        "priority": "Обычная",
                    },
                    "Глобальная проблема": {
                        "road_min": 20, "tech_min": 80, "docs_min": 0,
                        "base_norm_min": 100, "duration_on_site_min": 80,
                        "skill": "emergency",
                        "skill_name_ru": "Аварийные работы",
                        "priority": "Срочная",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (reference_dir / "geocode_fallback.json").write_text(
        json.dumps(
            {"_default": [55.7558, 37.6173], "Кузьминки": [55.705, 37.755]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return reference_dir


def test_load_reference_parses_norms_and_fallback(tmp_path):
    reference_dir = _write_reference(tmp_path)

    reference = load_reference(reference_dir)

    assert reference.norms["Подключение"].duration_on_site_min == 70
    assert reference.norms["Подключение"].skill_name_ru == "Работы на подключение и дозаказы"
    assert reference.norms["Глобальная проблема"].priority == "Срочная"
    assert reference.fallback_centroids["Кузьминки"] == (55.705, 37.755)
    assert reference.fallback_default == (55.7558, 37.6173)


SYNTHETIC_HEADER = (
    "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес;"
    "Подключение;Гигабитное подключение\n"
)


def _write_synthetic_csv(tmp_path: Path) -> Path:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = (
        SYNTHETIC_HEADER
        + "74198;Подключение;Конвергенция абонента;17.08.2026 20:00;17.08.2026 22:00;"
          "Кузьминки;Город Москва, пр-кт.Волгоградский, д. 128 к 5;FMC;Нет\n"
        + "305816952;Глобальная проблема;Информация;17.08.2026 0:01;17.08.2026 23:59;"
          "Гагаринский;г.Город Москва, пр-кт.Ленинский, д. 70/11;;Нет\n"
        + "Адрес Офиса;г. Москва, ул Юных Ленинцев, д 83с 4;;;;;;;\n"
    )
    (raw_dir / "vostok_synthetic.csv").write_text(content, encoding="utf-8")
    return raw_dir


def test_load_synthetic_orders_parses_rows_and_extracts_office(tmp_path):
    raw_dir = _write_synthetic_csv(tmp_path)

    orders, offices = load_synthetic_orders(raw_dir)

    assert len(orders) == 2
    first = orders[0]
    assert first.order_id == "74198"
    assert first.bk_type == "Подключение"
    assert first.district == "Кузьминки"
    assert first.start_dt == datetime(2026, 8, 17, 20, 0)
    assert first.end_dt == datetime(2026, 8, 17, 22, 0)
    assert first.gigabit is False
    assert first.source_file == "vostok_synthetic.csv"

    emergency = orders[1]
    assert emergency.start_dt == datetime(2026, 8, 17, 0, 1)
    assert emergency.end_dt == datetime(2026, 8, 17, 23, 59)

    assert len(offices) == 1
    assert offices[0].address == "г. Москва, ул Юных Ленинцев, д 83с 4"
    assert offices[0].source_file == "vostok_synthetic.csv"


HISTORICAL_HEADER = (
    "Заявка;Тип заявки BK;Статус BK;Тип заявки HD;Начало;Окончание;Район;"
    "Адрес;Бригада;Гигабитное подключение\n"
)


def _write_historical_csv(tmp_path: Path) -> Path:
    historical_dir = tmp_path / "reference" / "historical"
    historical_dir.mkdir(parents=True)
    content = (
        HISTORICAL_HEADER
        + "305894566;Подключение;Отправлена;Конвергенция абонента;"
          "17.08.2026 18:00;17.08.2026 20:00;Даниловский;"
          "Город Москва, проезд.3-й Павелецкий, д. 9, кв. 47;"
          "Капитанчук Александр;Нет\n"
        + "305885689;Подключение;Отменена;Конвергенция абонента;"
          "17.08.2026 12:00;17.08.2026 14:00;Зюзино;"
          "Город Москва, ул.Херсонская, д. 18, кв. 66;"
          "Прокопенко Вячеслав;Нет\n"
    )
    (historical_dir / "control_set_a.csv").write_text(content, encoding="utf-8")
    return historical_dir


def test_load_historical_control_keeps_all_statuses(tmp_path):
    historical_dir = _write_historical_csv(tmp_path)

    orders = load_historical_control(historical_dir)

    assert len(orders) == 2
    statuses = {o.status for o in orders}
    assert statuses == {"Отправлена", "Отменена"}

    cancelled = next(o for o in orders if o.status == "Отменена")
    assert cancelled.brigade == "Прокопенко Вячеслав"
    assert cancelled.order_id == "305885689"
