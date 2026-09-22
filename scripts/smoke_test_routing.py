"""Ручная проверка routing/distance_matrix.py на реальной сети
OpenRouteService. Не запускается через pytest — требует ORS_API_KEY в
.env и интернет.

Запуск из корня репозитория: python -m scripts.smoke_test_routing"""
from __future__ import annotations

import logging

from dotenv import load_dotenv

from routing.distance_matrix import DistanceMatrixBuilder

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Красная площадь -> МГУ (реальные координаты, ~8-9 км по прямой).
POINTS = [(55.7539, 37.6208), (55.7033, 37.5304)]

if __name__ == "__main__":
    load_dotenv()
    builder = DistanceMatrixBuilder()
    for profile in ("car", "foot", "bike", "transit"):
        result = builder.build(profile, POINTS)
        print(
            f"{profile:8s} source={result.source:8s} "
            f"distance_km={result.distance_km[(0, 1)]:.2f} "
            f"duration_min={result.duration_min[(0, 1)]:.1f}"
        )
