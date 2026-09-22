"""Матрица времени/расстояния между точками через OpenRouteService, с
диск-кэшем и haversine-фолбэком при недоступности сети/ключа."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).parent.parent / "data" / "reference" / "routing_cache.json"
ORS_MATRIX_URL = "https://api.openrouteservice.org/v2/matrix/{profile}"
ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"

# профиль движения -> ORS-профиль (для "transit" отдельного профиля нет —
# производится из "foot", см. build()).
PROFILE_ORS_NAME = {"car": "driving-car", "foot": "foot-walking", "bike": "cycling-regular"}

# Средняя скорость (км/ч) для haversine-фолбэка и для расчёта времени
# "Общественного транспорта" (для него нет профиля ни в ORS, ни в OSRM —
# берётся geometry-расстояние пешего маршрута с более высокой скоростью,
# отражающей автобус/метро).
FALLBACK_SPEED_KMH = {"car": 30.0, "foot": 5.0, "bike": 15.0, "transit": 15.0}
ROAD_INDIRECTNESS = 1.3  # поправка haversine на непрямоту реальных дорог

VEHICLE_TO_PROFILE = {
    "Автомобиль": "car",
    "Пешеход": "foot",
    "Велосипед": "bike",
    "Общественный транспорт": "transit",
}

# Лимит бесплатного тарифа ORS Matrix API: routes = sources × destinations,
# максимум 3500 за один запрос. Размер блока выбран так, чтобы C×C всегда
# укладывался в лимит при любом положении блока в сетке точек.
MAX_ROUTES_PER_REQUEST = 3500
CHUNK_REQUEST_DELAY_SECONDS = 0.5


def _chunk_indices(n: int, chunk_size: int) -> list[list[int]]:
    """Разбивает диапазон 0..n-1 на подряд идущие блоки не длиннее chunk_size."""
    return [list(range(i, min(i + chunk_size, n))) for i in range(0, n, chunk_size)]


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class MatrixResult:
    distance_km: dict[tuple[int, int], float]
    duration_min: dict[tuple[int, int], float]
    source: str


def _fallback_matrix(points: list[tuple[float, float]], profile: str) -> MatrixResult:
    speed = FALLBACK_SPEED_KMH[profile]
    distance_km: dict[tuple[int, int], float] = {}
    duration_min: dict[tuple[int, int], float] = {}
    for i, a in enumerate(points):
        for j, b in enumerate(points):
            if i == j:
                distance_km[(i, j)] = 0.0
                duration_min[(i, j)] = 0.0
                continue
            km = haversine_km(a, b) * ROAD_INDIRECTNESS
            distance_km[(i, j)] = km
            duration_min[(i, j)] = km / speed * 60
    return MatrixResult(distance_km=distance_km, duration_min=duration_min, source="fallback")


class DistanceMatrixBuilder:
    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        api_key: str | None = None,
        force_fallback: bool = False,
        max_routes_per_request: int = MAX_ROUTES_PER_REQUEST,
    ) -> None:
        self.cache_path = cache_path
        self.api_key = api_key if api_key is not None else os.environ.get("ORS_API_KEY")
        self.force_fallback = force_fallback or not self.api_key
        self.max_routes_per_request = max_routes_per_request
        self._cache = self._load_cache()
        self._made_geometry_request = False

    def _load_cache(self) -> dict:
        if self.cache_path.exists():
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _cache_key(profile: str, points: list[tuple[float, float]]) -> str:
        rounded = [f"{lat:.5f},{lon:.5f}" for lat, lon in points]
        return profile + "|" + ";".join(rounded)

    def build(self, profile: str, points: list[tuple[float, float]]) -> MatrixResult:
        if profile == "transit":
            foot_result = self.build("foot", points)
            speed = FALLBACK_SPEED_KMH["transit"]
            duration_min = {key: km / speed * 60 for key, km in foot_result.distance_km.items()}
            return MatrixResult(
                distance_km=foot_result.distance_km,
                duration_min=duration_min,
                source=foot_result.source,
            )

        key = self._cache_key(profile, points)
        if key in self._cache:
            cached = self._cache[key]
            return MatrixResult(
                distance_km={
                    tuple(int(x) for x in k.split(",")): v for k, v in cached["distance_km"].items()
                },
                duration_min={
                    tuple(int(x) for x in k.split(",")): v for k, v in cached["duration_min"].items()
                },
                source="cache",
            )

        if not self.force_fallback:
            result = self._build_via_ors(profile, points)
            if result is not None:
                self._cache[key] = {
                    "distance_km": {f"{i},{j}": v for (i, j), v in result.distance_km.items()},
                    "duration_min": {f"{i},{j}": v for (i, j), v in result.duration_min.items()},
                }
                self._save_cache()
                return result
            logger.warning("OpenRouteService недоступен для профиля %s — используем фолбэк", profile)

        return _fallback_matrix(points, profile)

    def _build_via_ors(self, profile: str, points: list[tuple[float, float]]) -> MatrixResult | None:
        ors_profile = PROFILE_ORS_NAME[profile]
        locations = [[lon, lat] for lat, lon in points]
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}

        chunk_size = max(1, math.isqrt(self.max_routes_per_request))
        blocks = _chunk_indices(len(points), chunk_size)

        distance_km: dict[tuple[int, int], float] = {}
        duration_min: dict[tuple[int, int], float] = {}

        first_request = True
        for row_block in blocks:
            for col_block in blocks:
                if not first_request:
                    time.sleep(CHUNK_REQUEST_DELAY_SECONDS)
                first_request = False

                body = {
                    "locations": locations,
                    "sources": row_block,
                    "destinations": col_block,
                    "metrics": ["distance", "duration"],
                    "units": "km",
                }
                try:
                    response = requests.post(
                        ORS_MATRIX_URL.format(profile=ors_profile),
                        json=body,
                        headers=headers,
                        timeout=30,
                    )
                    response.raise_for_status()
                    data = response.json()
                    distances = data["distances"]
                    durations = data["durations"]
                    # Проверяем форму ответа до индексации: битый ответ
                    # (даже с HTTP 200) должен давать fallback, а не падение.
                    if len(distances) != len(row_block) or len(durations) != len(row_block):
                        raise ValueError("ORS Matrix: неверное число строк в ответе")
                    if any(len(row) != len(col_block) for row in distances) or any(
                        len(row) != len(col_block) for row in durations
                    ):
                        raise ValueError("ORS Matrix: неверное число столбцов в ответе")
                except Exception as exc:
                    logger.warning(
                        "Запрос к OpenRouteService упал для профиля %s (блок %s×%s): %s",
                        profile,
                        row_block,
                        col_block,
                        exc,
                    )
                    return None

                for local_i, global_i in enumerate(row_block):
                    for local_j, global_j in enumerate(col_block):
                        dist_value = distances[local_i][local_j]
                        dur_value = durations[local_i][local_j]
                        distance_km[(global_i, global_j)] = (
                            dist_value if dist_value is not None else float("inf")
                        )
                        duration_min[(global_i, global_j)] = (
                            dur_value / 60.0 if dur_value is not None else float("inf")
                        )

        return MatrixResult(distance_km=distance_km, duration_min=duration_min, source="ors")

    def fetch_geometry(
        self, profile: str, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]] | None:
        """Реальная геометрия по дорогам для уже зафиксированного порядка
        точек одного маршрута (не матрица "всё ко всем"). None при
        недоступности ORS — вызывающий код сам решает, чем заменить
        (обычно прямой линией между теми же точками)."""
        if len(points) < 2:
            return None

        if profile == "transit":
            return self.fetch_geometry("foot", points)

        key = "geometry:" + self._cache_key(profile, points)
        if key in self._cache:
            return [tuple(pair) for pair in self._cache[key]]

        if self.force_fallback:
            return None

        geometry = self._fetch_geometry_via_ors(profile, points)
        if geometry is None:
            logger.warning(
                "OpenRouteService Directions недоступен для профиля %s — оставляем прямую линию",
                profile,
            )
            return None

        self._cache[key] = [list(pair) for pair in geometry]
        self._save_cache()
        return geometry

    def _fetch_geometry_via_ors(
        self, profile: str, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]] | None:
        if self._made_geometry_request:
            time.sleep(CHUNK_REQUEST_DELAY_SECONDS)
        self._made_geometry_request = True

        ors_profile = PROFILE_ORS_NAME[profile]
        coordinates = [[lon, lat] for lat, lon in points]
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        body = {"coordinates": coordinates}

        try:
            response = requests.post(
                ORS_DIRECTIONS_URL.format(profile=ors_profile),
                json=body,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            raw_coords = data["features"][0]["geometry"]["coordinates"]
        except Exception as exc:
            logger.warning(
                "Запрос к OpenRouteService Directions упал для профиля %s: %s", profile, exc
            )
            return None

        return [(lat, lon) for lon, lat in raw_coords]
