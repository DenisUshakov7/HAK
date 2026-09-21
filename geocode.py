"""Реальный геокодер: geopy/Nominatim с диск-кэшем и фолбэком на центроиды
районов, когда Nominatim недоступен.

Использование:
    from geocode import Geocoder
    geocoder = Geocoder()
    result = geocoder.geocode_address("Город Москва, ул.Окская, д. 32", "Кузьминки")
    result.lat, result.lon, result.source
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

USER_AGENT = "beeline-fsm-hackathon-etl"
DEFAULT_CACHE_PATH = Path(__file__).parent / "data" / "reference" / "geocode_cache.json"
DEFAULT_FALLBACK_PATH = Path(__file__).parent / "data" / "reference" / "geocode_fallback.json"

# Nominatim не находит адрес по слитным сокращениям ("ул.Окская"), ему нужны
# полные слова ("улица Окская"). Сокращения делятся на три группы:
#   - _ADDRESS_DROP_LABEL_ONLY: указатель отбрасывается, а следующее за ним
#     название или номер сохраняется ("г.Домодедово" -> "Домодедово").
#   - _ADDRESS_DROP_WITH_NUMBER: указатель субадреса (кв., стр.) отбрасывается
#     вместе с номером — иначе остаётся «повисший» номер ("д. 28 стр. 1" ->
#     "28 1"), который Nominatim разбирает хуже, чем просто "28".
#   - _ADDRESS_EXPAND_TOKENS: тип улицы раскрывается ("ул." -> "улица").
# Точка после токена необязательна (см. _token_pattern): в заявках она слитно
# с названием ("ул.Окская"), а в адресе офиса — через пробел ("ул Юных
# Ленинцев").
_ADDRESS_DROP_LABEL_ONLY = {"г", "д", "пгт"}
_ADDRESS_DROP_WITH_NUMBER = {"кв", "стр"}
_ADDRESS_EXPAND_TOKENS = {
    "б-р": "бульвар",
    "наб": "набережная",
    "обл": "область",
    "пер": "переулок",
    "пр-зд": "проезд",
    "пр-кт": "проспект",
    "проезд": "проезд",
    "ул": "улица",
    "ш": "шоссе",
}


def _token_pattern(token: str) -> str:
    """Токен, за которым — точка (поглощается) либо граница слова.

    \\b не годится: между точкой и пробелом (оба — не-\\w) границы нет, из-за
    чего точка не поглощалась бы и оставалась в тексте. Поэтому — явная
    альтернатива: либо точка сразу после токена (всегда съедается), либо
    следующий символ — не кириллическая буква (тогда "ул" не матчится
    внутри уже полного слова "улица", где после "ул" идёт "и")."""
    return re.escape(token) + r"(?:\.|(?![а-яёА-ЯЁ]))"


_ADDRESS_DROP_WITH_NUMBER_PATTERN = re.compile(
    r"(?:"
    + "|".join(
        _token_pattern(token)
        for token in sorted(_ADDRESS_DROP_WITH_NUMBER, key=len, reverse=True)
    )
    + r")\s*\d+[а-яА-Я]?"
)
_ADDRESS_ABBREVIATION_PATTERN = re.compile(
    "|".join(
        _token_pattern(token)
        for token in sorted(
            [*_ADDRESS_DROP_LABEL_ONLY, *_ADDRESS_EXPAND_TOKENS], key=len, reverse=True
        )
    )
)
_CITY_WORD_PATTERN = re.compile(r"(?:^|(?<=,\s))[Гг]ород\s+")


def normalize_address_for_nominatim(address: str) -> str:
    """Раскрывает адрес из формата датасета в формат, который надёжно
    находит Nominatim: убирает слово "Город"/"город", сокращения без
    геозначимой ценности вместе с номером, который они вводят (кв., стр.),
    сокращения-указатели без номера (г., д., пгт.), раскрывает сокращения
    типов улиц в полные слова (ул. -> улица и т.д.)."""

    def _replace_abbreviation(match: re.Match[str]) -> str:
        # group(0) может включать необязательную точку из _token_pattern
        # ("ул." или "ул") — словари же держат "голый" токен без точки.
        token = match.group(0).rstrip(".")
        if token in _ADDRESS_DROP_LABEL_ONLY:
            return ""
        return _ADDRESS_EXPAND_TOKENS[token] + " "

    without_city_word = _CITY_WORD_PATTERN.sub("", address)
    without_sub_address = _ADDRESS_DROP_WITH_NUMBER_PATTERN.sub("", without_city_word)
    expanded = _ADDRESS_ABBREVIATION_PATTERN.sub(_replace_abbreviation, without_sub_address)
    collapsed = re.sub(r"\s+", " ", expanded)
    collapsed = re.sub(r"\s*,\s*", ", ", collapsed)
    collapsed = re.sub(r",\s*,", ",", collapsed)
    return collapsed.strip(" ,")


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    source: str


class Geocoder:
    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        fallback_path: Path = DEFAULT_FALLBACK_PATH,
        force_fallback: bool = False,
    ) -> None:
        self.cache_path = cache_path
        self.force_fallback = force_fallback or os.environ.get("ETL_NO_NETWORK") == "1"
        self._cache: dict[str, list[float]] = self._load_cache()
        self._fallback_centroids, self._fallback_default = self._load_fallback(fallback_path)
        self._nominatim_geocode = None
        if not self.force_fallback:
            self._nominatim_geocode = self._build_nominatim_geocode()

    def _build_nominatim_geocode(self):
        from geopy.extra.rate_limiter import RateLimiter
        from geopy.geocoders import Nominatim

        geolocator = Nominatim(user_agent=USER_AGENT, timeout=10)
        return RateLimiter(
            geolocator.geocode, min_delay_seconds=1, max_retries=1, error_wait_seconds=2.0
        )

    def _load_cache(self) -> dict[str, list[float]]:
        if self.cache_path.exists():
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _load_fallback(fallback_path: Path) -> tuple[dict[str, tuple[float, float]], tuple[float, float]]:
        raw = json.loads(fallback_path.read_text(encoding="utf-8"))
        default = tuple(raw["_default"])
        centroids = {
            district: tuple(coords)
            for district, coords in raw.items()
            if not district.startswith("_")
        }
        return centroids, default

    def _fallback(self, district: str | None) -> GeocodeResult:
        if district and district in self._fallback_centroids:
            lat, lon = self._fallback_centroids[district]
            return GeocodeResult(lat=lat, lon=lon, source=f"fallback:{district}")
        lat, lon = self._fallback_default
        return GeocodeResult(lat=lat, lon=lon, source="fallback:default")

    def geocode_address(self, address: str, district: str | None = None) -> GeocodeResult:
        key = address.strip().lower()
        if key in self._cache:
            lat, lon = self._cache[key]
            return GeocodeResult(lat=lat, lon=lon, source="cache")

        if not self.force_fallback:
            location = self._geocode_via_nominatim(address)
            if location is not None:
                result = GeocodeResult(lat=location.latitude, lon=location.longitude, source="nominatim")
                self._cache[key] = [result.lat, result.lon]
                self._save_cache()
                return result
            logger.warning("Nominatim не вернул результат для %r — используем фолбэк", address)

        return self._fallback(district)

    def _geocode_via_nominatim(self, address: str):
        # RateLimiter сам глушит GeocoderServiceError, поэтому ловим Exception
        # целиком: любой сетевой сбой должен давать фолбэк, а не ронять ETL.
        try:
            return self._nominatim_geocode(normalize_address_for_nominatim(address))
        except Exception as exc:
            logger.warning("Nominatim упал для %r: %s — используем фолбэк", address, exc)
            return None
