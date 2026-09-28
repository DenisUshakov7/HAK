import json
from pathlib import Path

from geocode import Geocoder
from geocode import normalize_address_for_nominatim


def _write_fallback(tmp_path: Path) -> Path:
    fallback_path = tmp_path / "geocode_fallback.json"
    fallback_path.write_text(
        json.dumps(
            {"_default": [55.7558, 37.6173], "Кузьминки": [55.705, 37.755]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return fallback_path


def test_force_fallback_uses_district_centroid(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    geocoder = Geocoder(
        cache_path=cache_path, fallback_path=fallback_path, force_fallback=True
    )

    result = geocoder.geocode_address("Город Москва, ул.Окская, д. 32", "Кузьминки")

    assert result.lat == 55.705
    assert result.lon == 37.755
    assert result.source == "fallback:Кузьминки"


def test_force_fallback_unknown_district_uses_default(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    geocoder = Geocoder(
        cache_path=cache_path, fallback_path=fallback_path, force_fallback=True
    )

    result = geocoder.geocode_address("Неизвестный адрес", "Неизвестный район")

    assert result.lat == 55.7558
    assert result.lon == 37.6173
    assert result.source == "fallback:default"


def test_cache_hit_short_circuits_before_fallback(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    cache_path.write_text(
        json.dumps({"кэшированный адрес": [1.0, 2.0]}, ensure_ascii=False),
        encoding="utf-8",
    )
    geocoder = Geocoder(
        cache_path=cache_path, fallback_path=fallback_path, force_fallback=True
    )

    result = geocoder.geocode_address("Кэшированный Адрес")

    assert result.lat == 1.0
    assert result.lon == 2.0
    assert result.source == "cache"


def test_fallback_result_is_not_written_to_cache(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    geocoder = Geocoder(
        cache_path=cache_path, fallback_path=fallback_path, force_fallback=True
    )

    geocoder.geocode_address("Город Москва, ул.Окская, д. 32", "Кузьминки")

    assert not cache_path.exists()


def test_normalize_expands_street_abbreviations_and_drops_noise_words():
    result = normalize_address_for_nominatim(
        "Город Москва, пр-кт.Волгоградский, д. 128 к 5"
    )
    assert result == "Москва, проспект Волгоградский, 128 к 5"


def test_normalize_handles_non_moscow_town_abbreviation():
    result = normalize_address_for_nominatim("г.Домодедово, ул.Советская, д. 5")
    assert result == "Домодедово, улица Советская, 5"


def test_normalize_leaves_already_expanded_address_untouched():
    result = normalize_address_for_nominatim("Москва, улица Окская, 32")
    assert result == "Москва, улица Окская, 32"


def test_normalize_drops_structure_number_instead_of_leaving_it_dangling():
    # Реальная заявка из data/raw/vostok_synthetic.csv (order 74949). До
    # фикса "стр." отбрасывалось, а номер строения оставался висеть рядом с
    # номером дома ("28 1"), что Nominatim не может разобрать надёжнее, чем
    # адрес без номера строения вовсе.
    result = normalize_address_for_nominatim(
        "Город Москва, ул.Международная, д. 28 стр. 1"
    )
    assert result == "Москва, улица Международная, 28"


def test_normalize_drops_apartment_number_instead_of_leaving_it_dangling():
    result = normalize_address_for_nominatim(
        "Город Москва, проезд.3-й Павелецкий, д. 9, кв. 47"
    )
    assert result == "Москва, проезд 3-й Павелецкий, 9"


def test_geocode_address_sends_normalized_query_to_nominatim(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    geocoder = Geocoder(
        cache_path=cache_path, fallback_path=fallback_path, force_fallback=False
    )

    captured: dict[str, str] = {}

    class FakeLocation:
        latitude = 1.0
        longitude = 2.0

    def fake_nominatim_geocode(query: str):
        captured["query"] = query
        return FakeLocation()

    geocoder._nominatim_geocode = fake_nominatim_geocode

    result = geocoder.geocode_address("Город Москва, ул.Окская, д. 32")

    assert captured["query"] == "Москва, улица Окская, 32"
    assert result.source == "nominatim"
    assert result.lat == 1.0
    assert result.lon == 2.0


def test_normalize_handles_abbreviations_without_trailing_dot():
    # Реальный адрес офиса из data/raw/vostok_synthetic.csv (хвостовая
    # строка "Адрес Офиса") — в отличие от заявок того же файла, здесь
    # сокращения без точки, через пробел ("ул Юных", "д 83с"). До фикса
    # normalize_address_for_nominatim требовала точку и не трогала этот
    # формат вовсе — адрес офиса (стартовая точка инженеров!) молча падал
    # в наихудший fallback:default вместо реальных координат.
    result = normalize_address_for_nominatim("г. Москва, ул Юных Ленинцев, д 83с 4")
    assert result == "Москва, улица Юных Ленинцев, 83с 4"


def test_far_cache_hit_for_district_falls_back_to_centroid(tmp_path):
    # Однофамильная улица в другом городе: точка в кэше далеко от района.
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    cache_path.write_text(json.dumps({"ул.окская, д. 32": [55.50, 37.58]}), encoding="utf-8")
    geocoder = Geocoder(cache_path=cache_path, fallback_path=fallback_path, force_fallback=True)

    result = geocoder.geocode_address("ул.Окская, д. 32", "Кузьминки")

    assert (result.lat, result.lon) == (55.705, 37.755)
    assert result.source == "fallback:Кузьминки"


def test_far_nominatim_result_for_district_is_rejected_and_not_cached(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    geocoder = Geocoder(cache_path=cache_path, fallback_path=fallback_path, force_fallback=False)

    class Location:
        latitude, longitude = 55.50, 37.58

    geocoder._nominatim_geocode = lambda query: Location()

    result = geocoder.geocode_address("ул.Окская, д. 32", "Кузьминки")

    assert result.source == "fallback:Кузьминки"
    assert not cache_path.exists() or "ул.окская, д. 32" not in cache_path.read_text(encoding="utf-8")


def test_nearby_cache_hit_for_district_is_kept(tmp_path):
    fallback_path = _write_fallback(tmp_path)
    cache_path = tmp_path / "geocode_cache.json"
    cache_path.write_text(json.dumps({"ул.окская, д. 32": [55.71, 37.76]}), encoding="utf-8")
    geocoder = Geocoder(cache_path=cache_path, fallback_path=fallback_path, force_fallback=True)

    result = geocoder.geocode_address("ул.Окская, д. 32", "Кузьминки")

    assert (result.lat, result.lon) == (55.71, 37.76)
    assert result.source == "cache"
