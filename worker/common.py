import contextlib
import os
import traceback
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).resolve().parent.parent / ".env")   # no-op inside Docker


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                           prepare_threshold=None)


@contextlib.contextmanager
def job_run(name):
    """Logs the job in job_runs; failures appear on the admin's Today page."""
    with connect() as c:
        run_id = c.execute("INSERT INTO job_runs (job, status) VALUES (%s, 'running') RETURNING id",
                           (name,)).fetchone()["id"]
    stats = {"rows": 0}
    status, error = "ok", None
    try:
        yield stats
    except Exception:
        status, error = "failed", traceback.format_exc()[-3000:]
    with connect() as c:
        c.execute("""UPDATE job_runs SET finished_at = NOW(), status = %s,
                     rows_affected = %s, error = %s WHERE id = %s""",
                  (status, stats["rows"], error, run_id))
