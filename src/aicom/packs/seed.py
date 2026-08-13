"""Installs a pack's agent and schedules. Safe to run repeatedly."""

import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.domain.cron import next_fire
from aicom.packs import Pack, UnknownPack, get_pack
from aicom.store.db import make_engine, session_factory
from aicom.store.models import Agent, Schedule


def seed_pack(session: Session, pack: Pack, *, now: datetime) -> tuple[uuid.UUID, int]:
    """Upsert the pack's agent and schedules. Returns (agent id, schedules written).

    Schedules are matched by `(agent, schedule name)`. Renaming a `PackSchedule`
    between seeds therefore leaves the old row behind under its old name — still
    enabled, still firing on the old beat, since seeding never prunes. A pack
    maintainer who renames a schedule must remove the stale row by hand.
    """
    agent = session.scalar(select(Agent).where(Agent.name == pack.agent.name))
    if agent is None:
        agent = Agent(id=uuid.uuid4(), name=pack.agent.name)
        session.add(agent)
    agent.persona = pack.agent.persona
    agent.allowed_tools = list(pack.agent.allowed_tools)
    agent.gated_tools = list(pack.agent.gated_tools)
    agent.max_run_seconds = pack.agent.max_run_seconds
    agent.enabled = True
    session.flush()

    for definition in pack.schedules:
        schedule = session.scalar(
            select(Schedule).where(
                Schedule.agent_id == agent.id, Schedule.name == definition.name
            )
        )
        if schedule is None:
            schedule = Schedule(
                id=uuid.uuid4(),
                agent_id=agent.id,
                name=definition.name,
                next_due_at=next_fire(definition.cron, definition.timezone, now),
            )
            session.add(schedule)
        # next_due_at is set only on creation, never recomputed here on update.
        # Recomputing on every seed would push a schedule that is due right now
        # into the future, silently skipping a firing. The scheduler itself
        # advances next_due_at on each tick, so an edited cron takes effect
        # starting from the schedule's next natural firing rather than being
        # rebased immediately by a re-seed.
        schedule.cron = definition.cron
        schedule.timezone = definition.timezone
        schedule.title_template = definition.title_template
        schedule.goal_template = definition.goal_template
        session.flush()

    return agent.id, len(pack.schedules)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m aicom.packs.seed <pack>", file=sys.stderr)
        return 2
    try:
        pack = get_pack(argv[1])
    except UnknownPack as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Imported here, not at module load, so importing seed_pack (e.g. from tests)
    # never triggers settings resolution -- only running this CLI entry point does.
    from aicom.config import load_settings

    sessions = session_factory(make_engine(load_settings().database_url))
    with sessions() as session:
        agent_id, written = seed_pack(session, pack, now=datetime.now(UTC))
        session.commit()
    print(f"seeded pack {pack.name}: agent {agent_id}, {written} schedule(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
