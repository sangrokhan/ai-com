import logging
import time
from datetime import UTC, datetime, timedelta

from slack_sdk import WebClient

from aicom.config import Settings, load_settings
from aicom.executor.cli import ClaudeCliExecutor
from aicom.notify.slack import SlackNotifier
from aicom.orchestrator.scheduler import Scheduler
from aicom.orchestrator.sweeper import Sweeper
from aicom.orchestrator.worker import Worker
from aicom.store.db import make_engine, session_factory

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)


def run_worker_forever(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    sessions = session_factory(make_engine(settings.database_url))
    notifier = SlackNotifier(WebClient(token=settings.slack_bot_token), settings.slack_channel)
    worker = Worker(sessions, ClaudeCliExecutor(settings.claude_binary), notifier, settings, "w1")
    sweeper = Sweeper(sessions, notifier)
    scheduler = Scheduler(sessions, notifier)

    while True:
        now = datetime.now(UTC)
        # A single bad iteration (a DB hiccup, an executor crash surfaced as
        # an unhandled exception, etc.) must not take down the whole
        # background service -- there is no supervisor restarting this
        # process, so an uncaught exception here would silently stop every
        # queued task and every pending Slack reminder from ever being
        # picked up again. Log and keep looping; the next iteration retries.
        #
        # Each stage gets its own try/except so a failure in one (e.g. the
        # sweeper) cannot skip the others (e.g. the scheduler) within the
        # same iteration -- the scheduler's own per-schedule isolation would
        # otherwise be contingent on the sweeper's health.
        try:
            sweeper.recover_stale_runs(now, stale_after=STALE_AFTER)
            sweeper.sweep_reminders(now)
        except Exception:
            logger.exception("run_worker_forever: sweeper failed, continuing")

        try:
            scheduler.tick(now)
        except Exception:
            logger.exception("run_worker_forever: scheduler failed, continuing")

        try:
            did_work = worker.tick(now)
        except Exception:
            logger.exception("run_worker_forever: worker failed, continuing")
            did_work = False

        if not did_work:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run_worker_forever()
