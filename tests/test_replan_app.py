import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "ui" / "replan_app.py"
DATA = ROOT / "data" / "output"


@pytest.fixture(autouse=True)
def _no_ors(monkeypatch):
    # Пустой ключ: load_dotenv() его не перезапишет, DistanceMatrixBuilder
    # уйдёт в фолбэк — тесты не ходят в сеть.
    monkeypatch.setenv("ORS_API_KEY", "")


def _app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=120)


def test_app_starts_without_exception():
    at = _app().run()

    assert not at.exception
    assert at.title[0].value == "Перепланирование плана распределения"


def test_cancel_event_applies_and_shows_diff():
    at = _app().run()
    plan = json.loads((DATA / "assignment.json").read_text(encoding="utf-8"))
    order_id = plan["routes"][0]["stops"][-1]["order_id"]

    at.checkbox(key="no_network").check().run()
    at.selectbox(key="event_kind").select("Отмена заявки").run()
    at.text_input(key="event_time").set_value("00:00").run()
    at.selectbox(key="cancel_order").select(order_id).run()
    at.button(key="apply").click().run()

    assert not at.exception
    assert not at.error
    assert any(f"{order_id} отменена" in block.value for block in at.code)


def test_invalid_time_shows_error_and_keeps_plan():
    at = _app().run()

    at.checkbox(key="no_network").check().run()
    at.selectbox(key="event_kind").select("Инженер недоступен").run()
    at.text_input(key="event_time").set_value("не время").run()
    at.button(key="apply").click().run()

    assert not at.exception
    assert any("ЧЧ:ММ" in err.value for err in at.error)


def test_reset_button_returns_to_initial_state():
    at = _app().run()
    plan = json.loads((DATA / "assignment.json").read_text(encoding="utf-8"))
    order_id = plan["routes"][0]["stops"][-1]["order_id"]
    at.checkbox(key="no_network").check().run()
    at.selectbox(key="event_kind").select("Отмена заявки").run()
    at.text_input(key="event_time").set_value("00:00").run()
    at.selectbox(key="cancel_order").select(order_id).run()
    at.button(key="apply").click().run()

    at.button(key="reset").click().run()

    assert not at.exception
    assert any("ещё не применялось" in info.value for info in at.info)


def test_unavailable_with_interval_shows_note():
    at = _app().run()

    at.checkbox(key="no_network").check().run()
    at.selectbox(key="event_kind").select("Инженер недоступен").run()
    at.text_input(key="event_time").set_value("00:00").run()
    at.text_input(key="unavail_from").set_value("12:00").run()
    at.text_input(key="unavail_until").set_value("13:00").run()
    at.button(key="apply").click().run()

    assert not at.exception
    assert not at.error
    assert any("недоступен с 12:00 до 13:00" in info.value for info in at.info)
