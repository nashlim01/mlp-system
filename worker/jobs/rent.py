import datetime as dt
from zoneinfo import ZoneInfo

from common import connect, job_run

TZ = ZoneInfo("Asia/Kuching")


def _generate(period):
    with job_run(f"rent_schedule {period:%Y-%m}") as s, connect() as c:
        s["rows"] = c.execute("SELECT generate_rent_schedule(%s) AS n", (period,)).fetchone()["n"]
        c.execute("SELECT attach_utility_bills()")             # bills waiting for this month's line


def generate_this_month():
    _generate(dt.datetime.now(TZ).date().replace(day=1))


def generate_next_month():
    first = dt.datetime.now(TZ).date().replace(day=1)
    _generate((first + dt.timedelta(days=32)).replace(day=1))
