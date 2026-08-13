"""The opportunity research pack: watch a beat, report what changed."""

from aicom.packs.types import Pack, PackAgent, PackSchedule

PERSONA = """\
You watch a defined beat and report what changed.

Before writing anything, read every file in `previous/` if that directory
exists. Those are your own most recent reports, newest first. Whatever you
already told the operator, do not tell them again.

Report only what is new, what changed, and what disappeared. For each item say
what it is, why it might matter, and where you found it.

If nothing has changed since your last report, say exactly that in one line and
stop. An empty report is a correct answer, and it is far more useful than an
invented one. You will be asked this question every day; most days the honest
answer is short.

Write your report to `report.md` in your working directory.
"""

OPPORTUNITY = Pack(
    name="opportunity",
    agent=PackAgent(
        name="scout",
        persona=PERSONA,
        allowed_tools=("WebSearch", "WebFetch", "Read", "Write", "Glob", "Grep"),
        gated_tools=(),
        max_run_seconds=900,
    ),
    schedules=(
        PackSchedule(
            name="ai-agent-tooling",
            cron="0 9 * * 1-5",
            timezone="Asia/Seoul",
            title_template="Scan: AI agent tooling",
            goal_template=(
                "Watch the AI agent tooling space. Look for newly released or "
                "substantially changed frameworks, orchestration tools, and "
                "agent platforms, and for tools that were widely used and have "
                "gone quiet or been abandoned.\n\n"
                "Report what a solo builder running autonomous agents would "
                "want to know that they did not know yesterday."
            ),
        ),
    ),
)
