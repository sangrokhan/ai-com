"""Agent packs: an agent and its schedules, as data.

A pack is not a subsystem. Adding one is a new definition file plus a registry
entry, not a new mechanism.
"""

from aicom.packs.opportunity import OPPORTUNITY
from aicom.packs.types import Pack, PackAgent, PackSchedule, UnknownPack

__all__ = ["PACKS", "Pack", "PackAgent", "PackSchedule", "UnknownPack", "get_pack"]

PACKS: dict[str, Pack] = {OPPORTUNITY.name: OPPORTUNITY}


def get_pack(name: str) -> Pack:
    try:
        return PACKS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PACKS))
        raise UnknownPack(f"unknown pack {name!r}; known packs: {known}") from exc
