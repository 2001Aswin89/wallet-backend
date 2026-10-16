"""Background worker. Step 1: skeleton only. Jobs (reconciliation, flag explanations,
key cleanup) are registered in later steps."""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.migrate import run_migrations

log = logging.getLogger("wallet.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_migrations()
    sched = BlockingScheduler(timezone="Asia/Kolkata")
    log.info("worker started; no jobs registered yet")
    sched.start()


if __name__ == "__main__":
    main()
