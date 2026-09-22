"""Типы данных и загрузка data/output/{orders,engineers,offices}.json
для модуля распределения заявок."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def parse_hhmm(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def format_hhmm(total_minutes: int) -> str:
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


@dataclass(frozen=True)
class Order:
    id: str
    lat: float
    lon: float
    duration_min: int
    window_start: str
    window_end: str
    priority: str
    required_skill: str
    required_transport: str | None


@dataclass(frozen=True)
class Engineer:
    id: str
    name: str
    start_lat: float
    start_lon: float
    shift_start: str
    shift_end: str
    skills: list[str]
    vehicle: str


def load_orders(path: Path) -> list[Order]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Order(
            id=r["id"],
            lat=r["lat"],
            lon=r["lon"],
            duration_min=r["duration_min"],
            window_start=r["window_start"],
            window_end=r["window_end"],
            priority=r["priority"],
            required_skill=r["required_skill"],
            required_transport=r.get("required_transport"),
        )
        for r in raw
    ]


def load_engineers(path: Path) -> list[Engineer]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Engineer(
            id=r["id"],
            name=r["name"],
            start_lat=r["start_lat"],
            start_lon=r["start_lon"],
            shift_start=r["shift_start"],
            shift_end=r["shift_end"],
            skills=list(r["skills"]),
            vehicle=r["vehicle"],
        )
        for r in raw
    ]


def load_office_coords(path: Path) -> tuple[float, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not raw:
        raise ValueError(f"В {path} нет ни одного офиса")
    first = raw[0]
    return first["lat"], first["lon"]
