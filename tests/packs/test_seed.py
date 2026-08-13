from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.packs import PACKS, UnknownPack, get_pack
from aicom.packs.seed import seed_pack
from aicom.store.models import Agent, Schedule

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_the_opportunity_pack_is_registered() -> None:
    assert "opportunity" in PACKS
    assert get_pack("opportunity").name == "opportunity"


def test_unknown_pack_raises_listing_the_known_ones() -> None:
    with pytest.raises(UnknownPack, match="opportunity"):
        get_pack("nope")


def test_seeding_creates_the_agent_and_its_schedules(session: Session) -> None:
    pack = get_pack("opportunity")

    agent_id, written = seed_pack(session, pack, now=NOW)
    session.commit()

    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.name == pack.agent.name
    assert agent.enabled is True
    assert written == len(pack.schedules)

    schedules = list(session.scalars(select(Schedule).where(Schedule.agent_id == agent_id)))
    assert len(schedules) == len(pack.schedules)
    assert all(s.next_due_at > NOW for s in schedules)


def test_seeding_twice_does_not_duplicate(session: Session) -> None:
    pack = get_pack("opportunity")

    first_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()
    second_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()

    assert first_id == second_id
    assert session.query(Agent).count() == 1
    assert session.query(Schedule).count() == len(pack.schedules)


def test_seeding_again_updates_a_changed_definition(session: Session) -> None:
    pack = get_pack("opportunity")
    agent_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()

    agent = session.get(Agent, agent_id)
    assert agent is not None
    agent.persona = "clobbered by hand"
    session.commit()

    seed_pack(session, pack, now=NOW)
    session.commit()

    session.expire_all()
    assert session.get(Agent, agent_id).persona == pack.agent.persona


def test_reseeding_after_a_cron_edit_updates_cron_but_not_next_due_at(
    session: Session,
) -> None:
    # Recomputing next_due_at on every seed would push a schedule that is due
    # right now into the future and silently skip a firing, so it must be set
    # only when the schedule row is first created.
    pack = get_pack("opportunity")
    agent_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()

    original = session.scalar(
        select(Schedule).where(Schedule.agent_id == agent_id)
    )
    assert original is not None
    original_next_due_at = original.next_due_at
    original_name = original.name

    edited_schedule = replace(pack.schedules[0], cron="0 10 * * 1-5")
    edited_pack = replace(pack, schedules=(edited_schedule, *pack.schedules[1:]))

    seed_pack(session, edited_pack, now=NOW)
    session.commit()
    session.expire_all()

    updated = session.scalar(
        select(Schedule).where(Schedule.agent_id == agent_id, Schedule.name == original_name)
    )
    assert updated is not None
    assert updated.cron == "0 10 * * 1-5"
    assert updated.next_due_at == original_next_due_at


def test_the_pack_reaches_no_gated_action() -> None:
    # It spends nothing, publishes nothing, contacts nobody.
    pack = get_pack("opportunity")
    assert pack.agent.gated_tools == ()
    assert "Bash" not in pack.agent.allowed_tools


def test_the_persona_permits_reporting_nothing() -> None:
    # Without this an agent asked daily for findings invents them.
    persona = get_pack("opportunity").agent.persona.lower()
    assert "nothing" in persona
