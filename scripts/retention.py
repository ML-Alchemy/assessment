"""Hourly best-effort raw transcript retention sweep."""
import logging
import os
import time

import psycopg

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("retention")
DATABASE_URL = os.environ["DATABASE_URL"]
INTERVAL = max(60, int(os.getenv("RETENTION_INTERVAL_SECONDS", "3600")))

def sweep():
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "UPDATE calls SET transcript = NULL, status = CASE WHEN status IN ('done', 'failed') THEN status ELSE 'failed' END, error = CASE WHEN status IN ('done', 'failed') THEN error ELSE 'Raw transcript retention expired before processing completed' END WHERE received_at < now() - interval '90 days' AND transcript IS NOT NULL RETURNING id"
        ).fetchall()
        conn.commit()
    log.info("retention_sweep deleted_transcripts=%d", len(row))
    return len(row)

if __name__ == "__main__":
    while True:
        try: sweep()
        except Exception: log.exception("retention sweep failed")
        time.sleep(INTERVAL)
