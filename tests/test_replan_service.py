import json
from dataclasses import dataclass

import pytest

from geocode import GeocodeResult
from routing.distance_matrix import DistanceMatrixBuilder
from routing.models import Engineer, Order
from ui.replan_service import (
    KIND_CANCEL,
    KIND_UNAVAILABLE,
    KIND_URGENT,
    SKILLS,
    Inputs,
    apply_to_session,
    build_event,
    diff_text,
    load_inputs,
    metrics_rows,
    new_session,
    next_urgent_id,
    plan_order_ids,
    describe_diff,
    format_metric_value,
    metrics_table,
    render_map_html,
    reset_session,
    unavailability_note,
)

OFFICE_RECORD = {"source_file": "x.csv", "address": "тест", "lat": 55.70, "lon": 37.60, "geocode_source": "nominatim"}


@dataclass
class FakeGeocoder:
    result: GeocodeResult

    def geocode_address(self, address, district=None):
        return self.result


def test_build_event_urgent_geocodes_address():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.702, lon=37.602, source="nominatim"))

    event = build_event(
        KIND_URGENT, "11:00", address="Москва, Тестовая 1", skill=SKILLS[2],
        duration_min=25, window_start="11:00", window_end="18:00",
        new_order_id="urgent_1", geocoder=geocoder,
    )

    assert event.type == "new_urgent_order"
    assert event.event_time == "11:00"
    assert event.new_order == Order("urgent_1", 55.702, 37.602, 25, "11:00", "18:00", "Срочная", SKILLS[2], None)


def test_build_event_urgent_rejects_fallback_geocode():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="fallback:default"))

    with pytest.raises(ValueError, match="не найден"):
        build_event(KIND_URGENT, "11:00", address="абракадабра", geocoder=geocoder)


def test_build_event_urgent_without_geocoder_raises():
    with pytest.raises(ValueError, match="Геокодинг отключён"):
        build_event(KIND_URGENT, "11:00", address="Москва", geocoder=None)


def test_build_event_urgent_requires_address():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    with pytest.raises(ValueError, match="адрес"):
        build_event(KIND_URGENT, "11:00", address="  ", geocoder=geocoder)


def test_build_event_urgent_rejects_reversed_window():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    with pytest.raises(ValueError, match="Начало окна"):
        build_event(
            KIND_URGENT, "11:00", address="Москва", window_start="18:00", window_end="09:00",
            geocoder=geocoder,
        )


def test_build_event_rejects_bad_time():
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        build_event(KIND_CANCEL, "25:99", order_id="1")


def test_build_event_normalizes_short_time():
    event = build_event(KIND_CANCEL, "9:05", order_id="1")

    assert event.event_time == "09:05"


def test_build_event_cancel():
    event = build_event(KIND_CANCEL, "11:00", order_id="32807")

    assert event.type == "cancel_order"
    assert event.order_id == "32807"


def test_build_event_cancel_requires_order():
    with pytest.raises(ValueError, match="заявку"):
        build_event(KIND_CANCEL, "11:00", order_id=None)


def test_build_event_unavailable():
    event = build_event(KIND_UNAVAILABLE, "11:00", engineer_id="eng_03")

    assert event.type == "engineer_unavailable"
    assert event.engineer_id == "eng_03"


def test_build_event_unavailable_requires_engineer():
    with pytest.raises(ValueError, match="инженера"):
        build_event(KIND_UNAVAILABLE, "11:00", engineer_id=None)


def test_build_event_unknown_kind_raises():
    with pytest.raises(ValueError, match="something"):
        build_event("something", "11:00")


def test_load_inputs_reads_all_four_files(tmp_path):
    (tmp_path / "orders.json").write_text(
        json.dumps(
            [{
                "id": "1", "lat": 55.701, "lon": 37.601, "duration_min": 10, "window_start": "09:00",
                "window_end": "18:00", "priority": "Обычная", "required_skill": "Локальные работы",
                "required_transport": None,
            }],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "engineers.json").write_text(
        json.dumps(
            [{
                "id": "e1", "name": "Тест", "start_lat": 55.70, "start_lon": 37.60, "shift_start": "08:00",
                "shift_end": "20:00", "skills": ["Локальные работы"], "vehicle": "Пешеход",
            }],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "offices.json").write_text(json.dumps([OFFICE_RECORD]), encoding="utf-8")
    (tmp_path / "assignment.json").write_text(
        json.dumps({"routes": [], "unassigned": [], "metrics": {}}), encoding="utf-8"
    )

    inputs = load_inputs(
        tmp_path / "orders.json", tmp_path / "engineers.json", tmp_path / "offices.json",
        tmp_path / "assignment.json",
    )

    assert [o.id for o in inputs.orders] == ["1"]
    assert [e.id for e in inputs.engineers] == ["e1"]
    assert inputs.office == (55.70, 37.60)
    assert inputs.assignment == {"routes": [], "unassigned": [], "metrics": {}}


@pytest.mark.parametrize("bad", ["24:00", "12:60", "ab:cd", "", "12:5", None])
def test_build_event_rejects_malformed_times(bad):
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        build_event(KIND_CANCEL, bad, order_id="1")


@pytest.mark.parametrize("good,expected", [("00:00", "00:00"), ("23:59", "23:59"), (" 11:00 ", "11:00")])
def test_build_event_accepts_boundary_and_padded_times(good, expected):
    assert build_event(KIND_CANCEL, good, order_id="1").event_time == expected


@pytest.mark.parametrize("bad", [0, -5, 0.5])
def test_build_event_urgent_rejects_non_positive_duration(bad):
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    with pytest.raises(ValueError, match="больше нуля"):
        build_event(KIND_URGENT, "11:00", address="Москва", duration_min=bad, geocoder=geocoder)


def test_build_event_urgent_rejects_non_numeric_duration():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    with pytest.raises(ValueError, match="числом минут"):
        build_event(KIND_URGENT, "11:00", address="Москва", duration_min="abc", geocoder=geocoder)


def test_build_event_urgent_accepts_float_duration_from_number_input():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    event = build_event(KIND_URGENT, "11:00", address="Москва", duration_min=25.0, geocoder=geocoder)

    assert event.new_order.duration_min == 25


@pytest.mark.parametrize("start,end", [("09:00", "09:00"), ("xx", "18:00"), ("09:00", "yy")])
def test_build_event_urgent_rejects_bad_or_empty_window(start, end):
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="nominatim"))

    with pytest.raises(ValueError):
        build_event(KIND_URGENT, "11:00", address="Москва", window_start=start, window_end=end, geocoder=geocoder)


def test_build_event_urgent_accepts_cached_geocode_source():
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7, lon=37.6, source="cache"))

    event = build_event(KIND_URGENT, "11:00", address="Москва", geocoder=geocoder)

    assert event.new_order.lat == 55.7


OFFICE = (55.70, 37.60)


def _inputs():
    orders = [
        Order("1", 55.701, 37.601, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
        Order("2", 55.702, 37.602, 10, "09:00", "18:00", "Обычная", "Локальные работы", None),
    ]
    engineers = [
        Engineer("e1", "Первый", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
        Engineer("e2", "Второй", *OFFICE, "08:00", "20:00", ["Локальные работы"], "Пешеход"),
    ]
    stop = lambda oid, arr: {  # noqa: E731
        "order_id": oid, "arrival": arr, "travel_min": 10, "distance_km": 1.0, "reason": "Исходная причина",
    }
    assignment = {
        "routes": [
            {"engineer_id": "e1", "engineer_name": "Первый", "stops": [stop("1", "09:10")], "total_distance_km": 1.0},
            {"engineer_id": "e2", "engineer_name": "Второй", "stops": [stop("2", "09:10")], "total_distance_km": 1.0},
        ],
        "unassigned": [],
        "metrics": {"engineers_used": 2, "total_distance_km": 2.0, "orders_assigned": 2, "orders_unassigned": 0},
    }
    return Inputs(orders=orders, engineers=engineers, office=OFFICE, assignment=assignment)


def _builder(tmp_path):
    return DistanceMatrixBuilder(cache_path=tmp_path / "cache.json", force_fallback=True)


def test_new_session_starts_with_initial_plan_everywhere():
    inputs = _inputs()

    session = new_session(inputs.assignment)

    assert session.initial_plan is inputs.assignment
    assert session.current_plan is inputs.assignment
    assert session.previous_plan is inputs.assignment
    assert session.extra_orders == ()
    assert session.last_diff is None


def test_apply_cancel_moves_current_to_previous(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    event = build_event(KIND_CANCEL, "08:30", order_id="1")

    after = apply_to_session(session, event, inputs, _builder(tmp_path))

    assert after.previous_plan is inputs.assignment
    assert after.last_diff["cancelled"] == ["1"]
    assert "1" not in plan_order_ids(after.current_plan)
    assert after.current_plan["metrics"]["orders_assigned"] == 1


def test_events_chain_second_applies_to_result_of_first(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)

    after_first = apply_to_session(session, build_event(KIND_CANCEL, "08:30", order_id="1"), inputs, _builder(tmp_path))
    after_second = apply_to_session(
        after_first, build_event(KIND_CANCEL, "08:30", order_id="2"), inputs, _builder(tmp_path)
    )

    assert after_second.previous_plan is after_first.current_plan
    assert plan_order_ids(after_second.current_plan) == []
    assert after_second.current_plan["metrics"]["engineers_used"] == 0


def test_urgent_order_is_remembered_and_can_be_cancelled_later(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7015, lon=37.6015, source="nominatim"))
    event = build_event(
        KIND_URGENT, "08:30", address="Москва", new_order_id=next_urgent_id(session), geocoder=geocoder
    )

    after_urgent = apply_to_session(session, event, inputs, _builder(tmp_path))

    assert [o.id for o in after_urgent.extra_orders] == ["urgent_1"]
    assert "urgent_1" in plan_order_ids(after_urgent.current_plan)
    assert next_urgent_id(after_urgent) == "urgent_2"

    after_cancel = apply_to_session(
        after_urgent, build_event(KIND_CANCEL, "08:30", order_id="urgent_1"), inputs, _builder(tmp_path)
    )
    assert "urgent_1" not in plan_order_ids(after_cancel.current_plan)


def test_failed_event_raises_and_leaves_session_untouched(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)

    with pytest.raises(ValueError, match="ghost"):
        apply_to_session(session, build_event(KIND_CANCEL, "08:30", order_id="ghost"), inputs, _builder(tmp_path))

    assert session.current_plan is inputs.assignment
    assert session.last_diff is None


def test_reset_returns_to_initial_plan_and_forgets_urgent_orders(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7015, lon=37.6015, source="nominatim"))
    event = build_event(KIND_URGENT, "08:30", address="Москва", geocoder=geocoder)
    changed = apply_to_session(session, event, inputs, _builder(tmp_path))

    restored = reset_session(changed)

    assert restored.current_plan is inputs.assignment
    assert restored.extra_orders == ()
    assert restored.last_diff is None
    assert next_urgent_id(restored) == "urgent_1"


def test_plan_order_ids_lists_assigned_then_unassigned():
    plan = {
        "routes": [{"engineer_id": "e1", "stops": [{"order_id": "1"}, {"order_id": "2"}]}],
        "unassigned": [{"order_id": "3", "reason": "x"}],
    }

    assert plan_order_ids(plan) == ["1", "2", "3"]


def test_metrics_rows_pairs_before_and_after():
    before = {"metrics": {"engineers_used": 2, "total_distance_km": 10.0, "orders_assigned": 5, "orders_unassigned": 1}}
    after = {"metrics": {"engineers_used": 1, "total_distance_km": 6.5, "orders_assigned": 4, "orders_unassigned": 2}}

    rows = metrics_rows(before, after)

    assert rows == [
        ("Задействовано инженеров", 2, 1),
        ("Суммарный пробег, км", 10.0, 6.5),
        ("Назначено заявок", 5, 4),
        ("Не назначено заявок", 1, 2),
    ]


def test_diff_text_matches_cli_format():
    diff = {
        "newly_assigned": [], "newly_unassigned": [], "reassigned": [], "cancelled": ["1"], "routes_changed": [],
    }

    assert "1 отменена" in diff_text(diff)


def test_render_map_html_draws_urgent_order_from_session(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7015, lon=37.6015, source="nominatim"))
    event = build_event(KIND_URGENT, "08:30", address="Москва", geocoder=geocoder)
    after = apply_to_session(session, event, inputs, _builder(tmp_path))

    html = render_map_html(
        after.current_plan,
        list(inputs.orders) + list(after.extra_orders),
        inputs.engineers,
        inputs.office,
        force_fallback=True,
        cache_path=tmp_path / "geometry_cache.json",
    )

    assert "urgent_1" in html
    assert "Сводка по плану" in html
    assert "Сравнение с базовым вариантом" not in html


def test_render_map_html_does_not_touch_files_outside_tmp(tmp_path, monkeypatch):
    inputs = _inputs()
    monkeypatch.chdir(tmp_path)

    html = render_map_html(
        inputs.assignment, list(inputs.orders), inputs.engineers, inputs.office,
        force_fallback=True, cache_path=tmp_path / "geometry_cache.json",
    )

    assert "Первый" in html
    assert list(tmp_path.glob("*.html")) == []


def _diff(**kw):
    base = {"newly_assigned": [], "newly_unassigned": [], "reassigned": [], "cancelled": [], "routes_changed": []}
    base.update(kw)
    return base


_NAMES = {"e1": "Иванов Иван", "e2": "Петров Пётр"}
_ADDRESSES = {"7": "ул. Окская, 1", "urgent_1": "Москва, Тверская, 5"}


def test_describe_diff_new_order_names_engineer_time_and_position():
    plan = {"routes": [{"engineer_id": "e1", "stops": [{"order_id": "5"}, {"order_id": "urgent_1"}]}]}
    diff = _diff(newly_assigned=[{"order_id": "urgent_1", "engineer_id": "e1", "arrival": "15:14"}])

    lines = describe_diff(diff, plan, engineer_names=_NAMES, addresses=_ADDRESSES)

    assert lines == [
        "Срочная заявка urgent_1 (Москва, Тверская, 5) назначена инженеру Иванов Иван (e1): "
        "приедет в 15:14, станет 2-й из 2 в его маршруте."
    ]


def test_describe_diff_reassigned_says_from_whom_to_whom():
    plan = {"routes": [{"engineer_id": "e2", "stops": [{"order_id": "7"}]}]}
    diff = _diff(reassigned=[{"order_id": "7", "from_engineer_id": "e1", "to_engineer_id": "e2", "arrival": "13:00"}])

    lines = describe_diff(diff, plan, engineer_names=_NAMES, addresses=_ADDRESSES)

    assert lines == [
        "Заявка 7 (ул. Окская, 1) передана от инженера Иванов Иван (e1) инженеру Петров Пётр (e2): "
        "приедет в 13:00, станет 1-й из 1 в его маршруте."
    ]


def test_describe_diff_unassigned_gives_reason():
    diff = _diff(newly_unassigned=[{"order_id": "7", "reason": "Все подходящие инженеры заняты"}])

    lines = describe_diff(diff, {"routes": []}, engineer_names=_NAMES, addresses=_ADDRESSES)

    assert lines == ["Заявка 7 (ул. Окская, 1) осталась без исполнителя: Все подходящие инженеры заняты."]


def test_describe_diff_cancelled_names_owner_and_route_change():
    diff = _diff(
        cancelled=["7"],
        routes_changed=[{"engineer_id": "e1", "before_stops": ["7", "8"], "after_stops": ["8"]}],
    )

    lines = describe_diff(diff, {"routes": []}, engineer_names=_NAMES, addresses=_ADDRESSES)

    assert lines == [
        "Заявка 7 (ул. Окская, 1) отменена и убрана из маршрута инженера Иванов Иван (e1).",
        "Маршрут инженера Иванов Иван (e1): было 2, стало 1.",
    ]


def test_describe_diff_cancelled_unassigned_order_and_unknown_names():
    lines = describe_diff(_diff(cancelled=["99"]), {"routes": []}, engineer_names={}, addresses={})

    assert lines == ["Заявка 99 отменена (она и так не была назначена)."]


def test_describe_diff_empty_says_nothing_changed():
    assert describe_diff(_diff(), {"routes": []}, engineer_names={}, addresses={}) == ["Ничего не изменилось."]


def test_format_metric_value_ints_plain_floats_two_decimals():
    assert format_metric_value(11) == "11"
    assert format_metric_value(195.2100) == "195.21"


def test_metrics_table_has_strings_and_signed_delta():
    before = {"metrics": {"engineers_used": 11, "total_distance_km": 195.21, "orders_assigned": 56, "orders_unassigned": 10}}
    after = {"metrics": {"engineers_used": 11, "total_distance_km": 195.96, "orders_assigned": 57, "orders_unassigned": 9}}

    table = metrics_table(before, after)

    assert table["Метрика"][1] == "Суммарный пробег, км"
    assert table["До"] == ["11", "195.21", "56", "10"]
    assert table["После"] == ["11", "195.96", "57", "9"]
    assert table["Изменение"] == ["0", "+0.75", "+1", "-1"]


def test_load_inputs_reads_order_addresses(tmp_path):
    (tmp_path / "orders.json").write_text(
        json.dumps(
            [{
                "id": "1", "address": "ул. Окская, 1", "lat": 55.701, "lon": 37.601, "duration_min": 10,
                "window_start": "09:00", "window_end": "18:00", "priority": "Обычная",
                "required_skill": "Локальные работы", "required_transport": None,
            }],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "engineers.json").write_text("[]", encoding="utf-8")
    (tmp_path / "offices.json").write_text(json.dumps([OFFICE_RECORD]), encoding="utf-8")
    (tmp_path / "assignment.json").write_text(json.dumps({"routes": [], "unassigned": [], "metrics": {}}), encoding="utf-8")

    inputs = load_inputs(
        tmp_path / "orders.json", tmp_path / "engineers.json", tmp_path / "offices.json", tmp_path / "assignment.json"
    )

    assert inputs.addresses == {"1": "ул. Окская, 1"}


def test_apply_to_session_remembers_urgent_address(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    geocoder = FakeGeocoder(GeocodeResult(lat=55.7015, lon=37.6015, source="nominatim"))
    event = build_event(KIND_URGENT, "08:30", address="Москва, Тверская, 5", geocoder=geocoder)

    after = apply_to_session(session, event, inputs, _builder(tmp_path), address="Москва, Тверская, 5")

    assert after.extra_addresses == {"urgent_1": "Москва, Тверская, 5"}
    assert reset_session(after).extra_addresses == {}


def test_describe_diff_explains_idle_engineer_has_nothing_to_change():
    event = build_event(KIND_UNAVAILABLE, "10:00", engineer_id="e1")
    plan_before = {"routes": [{"engineer_id": "e2", "stops": [{"order_id": "7", "arrival": "12:00"}]}]}

    lines = describe_diff(
        _diff(), plan_before, engineer_names=_NAMES, addresses={}, event=event, plan_before=plan_before
    )

    assert lines == ["У инженера Иванов Иван (e1) в плане нет заявок — менять нечего."]


def test_describe_diff_explains_all_orders_already_started():
    event = build_event(KIND_UNAVAILABLE, "10:00", engineer_id="e1")
    plan_before = {"routes": [{"engineer_id": "e1", "stops": [{"order_id": "7", "arrival": "09:30"}]}]}

    lines = describe_diff(
        _diff(), plan_before, engineer_names=_NAMES, addresses={}, event=event, plan_before=plan_before
    )

    assert lines == [
        "Все заявки инженера Иванов Иван (e1) начаты раньше 10:00 — уже случившееся не меняется, менять нечего."
    ]


def test_build_event_unavailable_with_interval():
    event = build_event(
        KIND_UNAVAILABLE, "11:00", engineer_id="e1", unavailable_from="12:00", unavailable_until="13:30"
    )
    assert event.unavailable_from == "12:00"
    assert event.unavailable_until == "13:30"


def test_build_event_unavailable_empty_interval_means_defaults():
    event = build_event(KIND_UNAVAILABLE, "11:00", engineer_id="e1", unavailable_from="", unavailable_until=" ")
    assert event.unavailable_from is None
    assert event.unavailable_until is None


def test_build_event_unavailable_rejects_until_not_after_from():
    with pytest.raises(ValueError, match="позже"):
        build_event(
            KIND_UNAVAILABLE, "11:00", engineer_id="e1", unavailable_from="13:00", unavailable_until="12:00"
        )


def test_build_event_unavailable_rejects_bad_time_format():
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        build_event(KIND_UNAVAILABLE, "11:00", engineer_id="e1", unavailable_until="abc")


def test_unavailability_note_describes_interval():
    names = {"e1": "Иванов"}
    event = build_event(
        KIND_UNAVAILABLE, "11:00", engineer_id="e1", unavailable_from="12:00", unavailable_until="13:30"
    )
    assert unavailability_note(event, names) == "Инженер Иванов (e1) недоступен с 12:00 до 13:30."
    open_ended = build_event(KIND_UNAVAILABLE, "11:00", engineer_id="e1")
    assert unavailability_note(open_ended, names) == "Инженер Иванов (e1) недоступен с 11:00 до конца смены."
    assert unavailability_note(build_event(KIND_CANCEL, "11:00", order_id="1"), names) is None


def test_apply_to_session_remembers_blackout_and_reset_forgets_it(tmp_path):
    inputs = _inputs()
    session = new_session(inputs.assignment)
    event = build_event(
        KIND_UNAVAILABLE, "08:30", engineer_id="e1", unavailable_from="12:00", unavailable_until="13:00"
    )

    after = apply_to_session(session, event, inputs, _builder(tmp_path))

    assert after.blackouts == {"e1": [(720, 780)]}
    assert session.blackouts == {}
    assert reset_session(after).blackouts == {}


def test_nothing_changed_reason_for_interval_with_no_orders_in_window():
    plan_before = {"routes": [{"engineer_id": "e1", "stops": [{"order_id": "1", "arrival": "09:00"}]}]}
    event = build_event(
        KIND_UNAVAILABLE, "08:00", engineer_id="e1", unavailable_from="12:00", unavailable_until="13:00"
    )
    empty = {"newly_assigned": [], "newly_unassigned": [], "reassigned": [], "cancelled": [], "routes_changed": []}

    lines = describe_diff(
        empty, plan_before, engineer_names={"e1": "Иванов"}, addresses={}, event=event, plan_before=plan_before
    )

    assert lines == ["В интервале с 12:00 до 13:00 у инженера Иванов (e1) нет заявок — менять нечего."]


def test_describe_diff_same_engineer_reassignment_says_shifted():
    plan = {"routes": [{"engineer_id": "e1", "stops": [{"order_id": "2", "arrival": "12:10"}]}]}
    diff = {
        "newly_assigned": [], "newly_unassigned": [], "cancelled": [], "routes_changed": [],
        "reassigned": [{"order_id": "2", "from_engineer_id": "e1", "to_engineer_id": "e1", "arrival": "12:10"}],
    }

    lines = describe_diff(diff, plan, engineer_names={"e1": "Иванов"}, addresses={})

    assert len(lines) == 1
    assert "остаётся у инженера Иванов (e1)" in lines[0]
    assert "сдвинута на 12:10" in lines[0]


def test_render_map_html_shows_order_details_from_order_details(tmp_path):
    inputs = _inputs()

    html = render_map_html(
        inputs.assignment, list(inputs.orders), inputs.engineers, inputs.office,
        force_fallback=True, cache_path=tmp_path / "cache.json",
        order_details={"1": {"address": "Москва, ул. Пример, 5", "geocode_source": "fallback:Кузьминки"}},
    )

    assert "Адрес: Москва, ул. Пример, 5" in html
    assert "Тип работ: Локальные работы" in html
    assert "Координаты приблизительные" in html
