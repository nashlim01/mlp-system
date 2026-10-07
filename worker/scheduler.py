import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from jobs import electric, rent

TZ = "Asia/Kuching"
JOBS = {
    "rent_next_month": (rent.generate_next_month,      CronTrigger(day=25, hour=8, timezone=TZ)),
    "rent_this_month": (rent.generate_this_month,      CronTrigger(day=1, hour=0, minute=30, timezone=TZ)),
    "electric_check":  (electric.run,                  CronTrigger(hour=7, timezone=TZ)),
}

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":     # e.g. python scheduler.py --run rent_this_month
        JOBS[sys.argv[2]][0]()
        sys.exit()
    sched = BlockingScheduler(timezone=TZ)
    for name, (fn, trigger) in JOBS.items():
        sched.add_job(fn, trigger, id=name, misfire_grace_time=3600, coalesce=True)
    print("Scheduler started:", ", ".join(JOBS))
    sched.start()
