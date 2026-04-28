"""
Standalone review worker / scheduler.
Run independently from the Flask web app:

    python worker.py

Checks the database every 60 seconds for pending review jobs and processes
them sequentially, one business at a time.
"""

import time
import logging

from dotenv import load_dotenv

import db
import bot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds between checks


def process_pending_jobs():
    business_ids = db.get_businesses_with_pending()
    if not business_ids:
        log.info("No pending jobs found.")
        return

    for biz_id in business_ids:
        biz = db.get_business(biz_id)
        config = db.get_review_config(biz_id)
        if not biz or not config:
            log.warning("Skipping business_id=%s — missing data.", biz_id)
            continue

        delay = max(5, int(config.get("delay_seconds") or 60))
        log.info(
            "Processing business '%s' (id=%s) with %ds delay between reviews.",
            biz["name"], biz_id, delay,
        )
        bot.run_reviews(biz_id, biz, config, delay=delay)
        log.info("Finished business '%s' (id=%s).", biz["name"], biz_id)


def main():
    db.init_db()
    db.migrate_add_delay_seconds()
    db.reset_stuck_reviewing()
    log.info("Worker started. Reset any stuck 'reviewing' jobs to 'pending'.")
    log.info("Worker started. Polling every %ds for pending jobs.", POLL_INTERVAL)

    while True:
        try:
            process_pending_jobs()
        except Exception as exc:
            log.exception("Unexpected error during job processing: %s", exc)

        log.info("Sleeping %ds before next check...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
