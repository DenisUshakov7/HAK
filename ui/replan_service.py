"""Чистая логика интерфейса перепланирования (без импорта Streamlit —
тестируется обычным pytest): загрузка входов, сборка события из значений
формы, сессия с цепочкой событий, рендер карты."""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from routemap.build_map import run as build_map_run
from routing.distance_matrix import DEFAULT_CACHE_PATH, DistanceMatrixBuilder
from routing.models import Engineer, Order, load_engineers, load_office_coords, load_orders, format_hhmm, parse_hhmm
from routing.replan import ReplanEvent, _format_diff, apply_event, blackout_for_event

KIND_URGENT = "urgent"
KIND_CANCEL = "cancel"
KIND_UNAVAILABLE = "unavailable"

SKILLS = [
    "Локальные работы",
    "Работы на подключение и дозаказы",
    "Аварийные работы",
]

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Inputs:
    orders: list[Order]
    engineers: list[Engineer]
    office: tuple[float, float]
    assignment: dict
    addresses: dict[str, str] = field(default_factory=dict)


def load_inputs(
    orders_path: Path, engineers_path: Path, offices_path: Path, assignment_path: Path
) -> Inputs:
    raw_orders = json.loads(orders_path.read_text(encoding="utf-8"))
    return Inputs(
        addresses={r["id"]: r["address"] for r in raw_orders if r.get("address")},
        orders=load_orders(orders_path),
        engineers=load_engineers(engineers_path),
        office=load_office_coords(offices_path),
        assignment=json.loads(assignment_path.read_text(encoding="utf-8")),
    )


def _check_hhmm(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _HHMM.match(value):
        raise ValueError(f"{label}: ожидается формат ЧЧ:ММ, получено {value!r}")
    hours, minutes = value.split(":")
    return f"{int(hours):02d}:{minutes}"


def geocode_address(address: str, geocoder) -> tuple[float, float]:
    if geocoder is None:
        raise ValueError(
            "Геокодинг отключён (режим «без сети») — срочную заявку по адресу добавить нельзя"
        )
    if not (address or "").strip():
        raise ValueError("Укажите адрес срочной заявки")
    result = geocoder.geocode_address(address)
    if result.source.startswith("fallback"):
        raise ValueError(f"Адрес {address!r} не найден — уточните адрес")
    return result.lat, result.lon


def build_event(
    kind: str,
    event_time: str,
    *,
    address: str = "",
    skill: str = SKILLS[0],
    duration_min: int = 30,
    window_start: str = "09:00",
    window_end: str = "18:00",
    order_id: str | None = None,
    engineer_id: str | None = None,
    new_order_id: str = "urgent_1",
    unavailable_from: str | None = None,
    unavailable_until: str | None = None,
    geocoder=None,
) -> ReplanEvent:
    time = _check_hhmm(event_time, "Время события")

    if kind == KIND_URGENT:
        start = _check_hhmm(window_start, "Начало окна")
        end = _check_hhmm(window_end, "Конец окна")
        if parse_hhmm(start) >= parse_hhmm(end):
            raise ValueError("Начало окна должно быть раньше его конца")
        try:
            duration = int(duration_min)
        except (TypeError, ValueError):
            raise ValueError(f"Длительность должна быть числом минут, получено {duration_min!r}") from None
        if duration <= 0:
            raise ValueError("Длительность должна быть больше нуля")
        lat, lon = geocode_address(address, geocoder)
        new_order = Order(new_order_id, lat, lon, duration, start, end, "Срочная", skill, None)
        return ReplanEvent(type="new_urgent_order", event_time=time, new_order=new_order)

    if kind == KIND_CANCEL:
        if not order_id:
            raise ValueError("Выберите заявку для отмены")
        return ReplanEvent(type="cancel_order", event_time=time, order_id=order_id)

    if kind == KIND_UNAVAILABLE:
        if not engineer_id:
            raise ValueError("Выберите инженера")
        from_value = (unavailable_from or "").strip()
        until_value = (unavailable_until or "").strip()
        event = ReplanEvent(
            type="engineer_unavailable",
            event_time=time,
            engineer_id=engineer_id,
            unavailable_from=_check_hhmm(from_value, "Недоступен с") if from_value else None,
            unavailable_until=_check_hhmm(until_value, "Недоступен до") if until_value else None,
        )
        blackout_for_event(event)  # валидация интервала (ValueError)
        return event

    raise ValueError(f"Неизвестный тип события: {kind!r}")


@dataclass(frozen=True)
class Session:
    initial_plan: dict
    current_plan: dict
    previous_plan: dict
    extra_orders: tuple[Order, ...] = ()
    last_diff: dict | None = None
    urgent_counter: int = 0
    extra_addresses: dict[str, str] = field(default_factory=dict)
    last_event: ReplanEvent | None = None
    blackouts: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


def new_session(assignment: dict) -> Session:
    return Session(initial_plan=assignment, current_plan=assignment, previous_plan=assignment)


def next_urgent_id(session: Session) -> str:
    return f"urgent_{session.urgent_counter + 1}"


def apply_to_session(
    session: Session,
    event: ReplanEvent,
    inputs: Inputs,
    builder: DistanceMatrixBuilder,
    address: str | None = None,
) -> Session:
    """Применяет событие к ТЕКУЩЕМУ плану сессии (события можно применять
    подряд). Срочные заявки, добавленные ранее в этой сессии, подмешиваются
    к orders из orders.json — самого файла на диске это не касается. При
    ошибке ядра (ValueError) исключение пробрасывается, а исходная сессия
    не меняется (Session неизменяема)."""
    all_orders = list(inputs.orders) + list(session.extra_orders)
    plan, diff = apply_event(
        event, all_orders, inputs.engineers, inputs.office, session.current_plan, builder,
        blackouts=session.blackouts,
    )
    blackouts = {eid: list(windows) for eid, windows in session.blackouts.items()}
    window = blackout_for_event(event)
    if window is not None:
        blackouts.setdefault(window[0], []).append((window[1], window[2]))
    extra_orders = session.extra_orders
    urgent_counter = session.urgent_counter
    extra_addresses = dict(session.extra_addresses)
    if event.new_order is not None:
        extra_orders = extra_orders + (event.new_order,)
        urgent_counter += 1
        if address:
            extra_addresses[event.new_order.id] = address
    return replace(
        session,
        previous_plan=session.current_plan,
        current_plan=plan,
        extra_orders=extra_orders,
        last_diff=diff,
        urgent_counter=urgent_counter,
        extra_addresses=extra_addresses,
        last_event=event,
        blackouts=blackouts,
    )


def reset_session(session: Session) -> Session:
    return new_session(session.initial_plan)


def plan_order_ids(plan: dict) -> list[str]:
    assigned = [stop["order_id"] for route in plan["routes"] for stop in route["stops"]]
    unassigned = [item["order_id"] for item in plan["unassigned"]]
    return assigned + unassigned


_METRIC_LABELS = [
    ("engineers_used", "Задействовано инженеров"),
    ("total_distance_km", "Суммарный пробег, км"),
    ("orders_assigned", "Назначено заявок"),
    ("orders_unassigned", "Не назначено заявок"),
]


def metrics_rows(before: dict, after: dict) -> list[tuple[str, object, object]]:
    return [(label, before["metrics"][key], after["metrics"][key]) for key, label in _METRIC_LABELS]


def diff_text(diff: dict) -> str:
    return _format_diff(diff)


def render_map_html(
    plan: dict,
    orders: list[Order],
    engineers: list[Engineer],
    office: tuple[float, float],
    *,
    force_fallback: bool,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> str:
    """Рендерит карту плана в HTML-строку через существующий
    routemap.build_map.run: входы пишутся во временный каталог (в orders
    — и срочные заявки сессии, которых нет в orders.json на диске),
    каталог удаляется после чтения результата. Блок сравнения с базовым
    вариантом не добавляется — comparison_path указывает на несуществующий
    файл."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orders_path = tmp_path / "orders.json"
        engineers_path = tmp_path / "engineers.json"
        offices_path = tmp_path / "offices.json"
        assignment_path = tmp_path / "assignment.json"
        orders_path.write_text(json.dumps([asdict(o) for o in orders], ensure_ascii=False), encoding="utf-8")
        engineers_path.write_text(
            json.dumps([asdict(e) for e in engineers], ensure_ascii=False), encoding="utf-8"
        )
        offices_path.write_text(json.dumps([{"lat": office[0], "lon": office[1]}]), encoding="utf-8")
        assignment_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

        output = build_map_run(
            orders_path=orders_path,
            engineers_path=engineers_path,
            offices_path=offices_path,
            assignment_path=assignment_path,
            output_path=tmp_path / "map.html",
            force_fallback=force_fallback,
            cache_path=cache_path,
            comparison_path=tmp_path / "no_comparison.json",
        )
        return output.read_text(encoding="utf-8")


def format_metric_value(value: object) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _format_delta(before: object, after: object) -> str:
    delta = after - before
    if isinstance(delta, float):
        return f"{delta:+.2f}" if abs(delta) >= 0.005 else "0"
    return f"{delta:+d}" if delta else "0"


def metrics_table(before: dict, after: dict) -> dict[str, list[str]]:
    """Колонки таблицы метрик уже строками (иначе pandas в Streamlit
    приводит целые к float и показывает «11.0000»), плюс колонка изменения."""
    rows = metrics_rows(before, after)
    return {
        "Метрика": [label for label, _, _ in rows],
        "До": [format_metric_value(b) for _, b, _ in rows],
        "После": [format_metric_value(a) for _, _, a in rows],
        "Изменение": [_format_delta(b, a) for _, b, a in rows],
    }


def order_label(order_id: str, addresses: dict[str, str]) -> str:
    address = addresses.get(order_id)
    return f"{order_id} ({address})" if address else order_id


def _engineer_label(engineer_id: str, names: dict[str, str]) -> str:
    name = names.get(engineer_id)
    return f"{name} ({engineer_id})" if name else engineer_id


def _position_phrase(plan: dict, engineer_id: str, order_id: str) -> str:
    for route in plan["routes"]:
        if route["engineer_id"] == engineer_id:
            ids = [stop["order_id"] for stop in route["stops"]]
            if order_id in ids:
                return f", станет {ids.index(order_id) + 1}-й из {len(ids)} в его маршруте"
    return ""


def unavailability_note(event: ReplanEvent | None, engineer_names: dict[str, str]) -> str | None:
    """Строка «Инженер … недоступен с … до …» для события engineer_unavailable."""
    window = blackout_for_event(event) if event is not None else None
    if window is None:
        return None
    engineer_id, from_min, until_min = window
    until_text = "конца смены" if until_min == 1440 else format_hhmm(until_min)
    return (
        f"Инженер {_engineer_label(engineer_id, engineer_names)} недоступен "
        f"с {format_hhmm(from_min)} до {until_text}."
    )


def _nothing_changed_reason(
    event: ReplanEvent | None, plan_before: dict | None, engineer_names: dict[str, str]
) -> str:
    if event is not None and event.type == "engineer_unavailable" and plan_before is not None:
        who = _engineer_label(event.engineer_id, engineer_names)
        route = next((r for r in plan_before["routes"] if r["engineer_id"] == event.engineer_id), None)
        if route is None or not route["stops"]:
            return f"У инженера {who} в плане нет заявок — менять нечего."
        if event.unavailable_until:
            from_text = event.unavailable_from or event.event_time
            return (
                f"В интервале с {from_text} до {event.unavailable_until} у инженера {who} "
                f"нет заявок — менять нечего."
            )
        return (
            f"Все заявки инженера {who} начаты раньше {event.event_time} — "
            f"уже случившееся не меняется, менять нечего."
        )
    return "Ничего не изменилось."


def describe_diff(
    diff: dict,
    plan: dict,
    *,
    engineer_names: dict[str, str],
    addresses: dict[str, str],
    event: ReplanEvent | None = None,
    plan_before: dict | None = None,
) -> list[str]:
    """Дифф перепланирования человеческим языком: кому ушла заявка, во
    сколько приедет, какой по счёту в маршруте, у кого сократился/вырос
    маршрут. plan — план ПОСЛЕ события (для позиции в маршруте)."""
    lines: list[str] = []
    for item in diff["newly_assigned"]:
        lines.append(
            f"Срочная заявка {order_label(item['order_id'], addresses)} назначена инженеру "
            f"{_engineer_label(item['engineer_id'], engineer_names)}: приедет в {item['arrival']}"
            f"{_position_phrase(plan, item['engineer_id'], item['order_id'])}."
        )
    for item in diff["reassigned"]:
        if item["from_engineer_id"] == item["to_engineer_id"]:
            lines.append(
                f"Заявка {order_label(item['order_id'], addresses)} остаётся у инженера "
                f"{_engineer_label(item['to_engineer_id'], engineer_names)}, но сдвинута "
                f"на {item['arrival']}{_position_phrase(plan, item['to_engineer_id'], item['order_id'])}."
            )
            continue
        lines.append(
            f"Заявка {order_label(item['order_id'], addresses)} передана от инженера "
            f"{_engineer_label(item['from_engineer_id'], engineer_names)} инженеру "
            f"{_engineer_label(item['to_engineer_id'], engineer_names)}: приедет в {item['arrival']}"
            f"{_position_phrase(plan, item['to_engineer_id'], item['order_id'])}."
        )
    for item in diff["newly_unassigned"]:
        lines.append(
            f"Заявка {order_label(item['order_id'], addresses)} осталась без исполнителя: {item['reason']}."
        )
    for order_id in diff["cancelled"]:
        owner = next(
            (r["engineer_id"] for r in diff["routes_changed"] if order_id in r["before_stops"]), None
        )
        label = order_label(order_id, addresses)
        if owner is not None:
            lines.append(
                f"Заявка {label} отменена и убрана из маршрута инженера "
                f"{_engineer_label(owner, engineer_names)}."
            )
        else:
            lines.append(f"Заявка {label} отменена (она и так не была назначена).")
    for route in diff["routes_changed"]:
        lines.append(
            f"Маршрут инженера {_engineer_label(route['engineer_id'], engineer_names)}: "
            f"было {len(route['before_stops'])}, стало {len(route['after_stops'])}."
        )
    return lines or [_nothing_changed_reason(event, plan_before, engineer_names)]
