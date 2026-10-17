"""Background worker: runs the nightly pipeline at 02:00 IST."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.migrate import run_migrations
from app.worker.jobs import nightly

log = logging.getLogger("wallet.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_migrations()
    sched = BlockingScheduler(timezone="Asia/Kolkata")
    sched.add_job(nightly, CronTrigger(hour=2, minute=0, timezone="Asia/Kolkata"), id="nightly",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    log.info("worker started; nightly pipeline scheduled for 02:00 Asia/Kolkata")
    sched.start()


if __name__ == "__main__":
    main()
