"""Streamlit-интерфейс перепланирования (ТЗ §3.2 допускает Streamlit как
упрощённый MVP-интерфейс). Тонкая оболочка над ui/replan_service.py —
вся логика там. Запуск: streamlit run ui/replan_app.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import streamlit.components.v1 as components  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from geocode import Geocoder  # noqa: E402
from routing.distance_matrix import DistanceMatrixBuilder  # noqa: E402
from ui import replan_service as svc  # noqa: E402

DATA = ROOT / "data" / "output"
KIND_LABELS = {
    "Срочная заявка": svc.KIND_URGENT,
    "Отмена заявки": svc.KIND_CANCEL,
    "Инженер недоступен": svc.KIND_UNAVAILABLE,
}

load_dotenv()
# В облаке секрет приходит через st.secrets, а не .env — переносим его в
# os.environ, если он ещё не задан. Без ключа работает haversine-фолбэк.
if not os.environ.get("ORS_API_KEY"):
    try:
        secret = st.secrets.get("ORS_API_KEY")
    except Exception:
        secret = None
    if secret:
        os.environ["ORS_API_KEY"] = secret
st.set_page_config(page_title="Перепланирование", layout="wide")


def _init_state() -> None:
    if "inputs" in st.session_state:
        return
    inputs = svc.load_inputs(
        DATA / "orders.json", DATA / "engineers.json", DATA / "offices.json", DATA / "assignment.json"
    )
    st.session_state.inputs = inputs
    st.session_state.session = svc.new_session(inputs.assignment)
    st.session_state.maps = None


def _maps(no_network: bool) -> dict:
    cached = st.session_state.maps
    if cached is not None and cached["no_network"] == no_network:
        return cached
    inputs = st.session_state.inputs
    session = st.session_state.session
    orders = list(inputs.orders) + list(session.extra_orders)

    def render(plan: dict) -> str:
        return svc.render_map_html(
            plan, orders, inputs.engineers, inputs.office, force_fallback=no_network
        )

    st.session_state.maps = {
        "no_network": no_network,
        "after": render(session.current_plan),
        "before": render(session.previous_plan),
    }
    return st.session_state.maps


def _apply_event(kind: str, no_network: bool) -> None:
    inputs = st.session_state.inputs
    session = st.session_state.session
    geocoder = Geocoder() if (kind == svc.KIND_URGENT and not no_network) else None
    event = svc.build_event(
        kind,
        st.session_state.event_time,
        address=st.session_state.get("address", ""),
        skill=st.session_state.get("skill", svc.SKILLS[0]),
        duration_min=int(st.session_state.get("duration", 30)),
        window_start=st.session_state.get("window_start", "09:00"),
        window_end=st.session_state.get("window_end", "18:00"),
        order_id=st.session_state.get("cancel_order"),
        engineer_id=st.session_state.get("unavail_engineer"),
        new_order_id=svc.next_urgent_id(session),
        unavailable_from=st.session_state.get("unavail_from", ""),
        unavailable_until=st.session_state.get("unavail_until", ""),
        geocoder=geocoder,
    )
    builder = DistanceMatrixBuilder(force_fallback=no_network)
    st.session_state.session = svc.apply_to_session(
        session,
        event,
        inputs,
        builder,
        address=st.session_state.get("address") if kind == svc.KIND_URGENT else None,
    )
    st.session_state.maps = None


_init_state()
inputs = st.session_state.inputs
session = st.session_state.session

st.title("Перепланирование плана распределения")

with st.sidebar:
    st.header("Событие")
    kind_label = st.selectbox("Тип события", list(KIND_LABELS), key="event_kind")
    kind = KIND_LABELS[kind_label]
    st.text_input("Время события (ЧЧ:ММ)", value="11:00", key="event_time")

    if kind == svc.KIND_URGENT:
        st.text_input("Адрес заявки", key="address", placeholder="Москва, ул. Окская, 1")
        st.selectbox("Требуемый навык", svc.SKILLS, key="skill")
        st.number_input("Длительность, мин", min_value=5, max_value=480, value=30, key="duration")
        st.text_input("Окно: начало (ЧЧ:ММ)", value="11:00", key="window_start")
        st.text_input("Окно: конец (ЧЧ:ММ)", value="18:00", key="window_end")
    elif kind == svc.KIND_CANCEL:
        st.selectbox("Заявка", svc.plan_order_ids(session.current_plan), key="cancel_order")
    else:
        names = {e.id: e.name for e in inputs.engineers}
        st.selectbox(
            "Инженер", list(names), format_func=lambda eid: f"{names[eid]} ({eid})", key="unavail_engineer"
        )
        st.text_input("Недоступен с (пусто = время события)", key="unavail_from", placeholder="12:00")
        st.text_input("Недоступен до (пусто = до конца смены)", key="unavail_until", placeholder="13:30")

    no_network = st.checkbox(
        "Без сети (приближённые расстояния, без геокодинга)", value=False, key="no_network"
    )
    apply_clicked = st.button("Перестроить", type="primary", key="apply")
    reset_clicked = st.button("Сбросить к исходному плану", key="reset")

if reset_clicked:
    st.session_state.session = svc.reset_session(session)
    st.session_state.maps = None
    st.rerun()

if apply_clicked:
    try:
        _apply_event(kind, no_network)
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.rerun()

session = st.session_state.session

if session.last_diff is None:
    st.info("Событий ещё не применялось — показан исходный план.")
else:
    st.subheader("Что изменилось")
    addresses = {**inputs.addresses, **session.extra_addresses}
    names = {e.id: e.name for e in inputs.engineers}
    note = svc.unavailability_note(session.last_event, names)
    if note:
        st.info(note)
    for line in svc.describe_diff(
        session.last_diff,
        session.current_plan,
        engineer_names=names,
        addresses=addresses,
        event=session.last_event,
        plan_before=session.previous_plan,
    ):
        st.markdown(f"- {line}")
    with st.expander("Технический вид (как в CLI)"):
        st.code(svc.diff_text(session.last_diff))

st.table(svc.metrics_table(session.previous_plan, session.current_plan))

if session.current_plan["unassigned"]:
    with st.expander(f"Неназначенные заявки ({len(session.current_plan['unassigned'])})"):
        for item in session.current_plan["unassigned"]:
            all_addresses = {**inputs.addresses, **session.extra_addresses}
            st.write(f"**{svc.order_label(item['order_id'], all_addresses)}** — {item['reason']}")

maps = _maps(no_network)
tab_after, tab_before = st.tabs(["После", "До"])
with tab_after:
    components.html(maps["after"], height=700, scrolling=True)
with tab_before:
    components.html(maps["before"], height=700, scrolling=True)
