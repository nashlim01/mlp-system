import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from common import connect
from jobs import electric, rent

TZ = "Asia/Kuching"
JOBS = {
    "rent_next_month": (rent.generate_next_month,      CronTrigger(day=25, hour=8, timezone=TZ)),
    "rent_this_month": (rent.generate_this_month,      CronTrigger(day=1, hour=0, minute=30, timezone=TZ)),
    "electric_check":  (electric.run,                  CronTrigger(hour=7, timezone=TZ)),
}


def run_requests():
    """Jobs staff asked for from the portal (e.g. Utilities → Check SEB now)."""
    with connect() as c:
        req = c.execute("""UPDATE job_requests SET picked_at = NOW()
                           WHERE id = (SELECT id FROM job_requests WHERE picked_at IS NULL
                                       ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                           RETURNING job""").fetchone()
    if req and req["job"] in JOBS:
        JOBS[req["job"]][0]()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":     # e.g. python scheduler.py --run rent_this_month
        JOBS[sys.argv[2]][0]()
        sys.exit()
    sched = BlockingScheduler(timezone=TZ)
    for name, (fn, trigger) in JOBS.items():
        sched.add_job(fn, trigger, id=name, misfire_grace_time=3600, coalesce=True)
    sched.add_job(run_requests, IntervalTrigger(minutes=1), id="requests", max_instances=1, coalesce=True)
    print("Scheduler started:", ", ".join(JOBS))
    sched.start()
