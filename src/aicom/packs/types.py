"""The shape of a pack. Kept apart from the registry to avoid an import cycle."""

from dataclasses import dataclass


class UnknownPack(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PackAgent:
    name: str
    persona: str
    allowed_tools: tuple[str, ...]
    gated_tools: tuple[str, ...] = ()
    max_run_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class PackSchedule:
    name: str
    cron: str
    timezone: str
    title_template: str
    goal_template: str


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    agent: PackAgent
    schedules: tuple[PackSchedule, ...]
