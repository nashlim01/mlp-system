"""Daily Sarawak Energy check. Replace every TODO with selectors from playwright codegen."""
import contextlib
import datetime as dt
import os
import pathlib
import re
import sys

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import connect, job_run

RAW = pathlib.Path("raw/electric")
STATE = pathlib.Path("state/seb_state.json")


def _login_if_needed(page):
    page.goto(os.environ["SEB_LOGIN_URL"])
    if page.locator("TODO: selector only present when logged out").count():
        page.fill("TODO: username field", os.environ["SEB_USERNAME"])
        page.fill("TODO: password field", os.environ["SEB_PASSWORD"])
        page.click("TODO: login button")
        page.wait_for_load_state("networkidle")


def fetch(accounts, day_dir):
    day_dir.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(STATE) if STATE.exists() else None)
        page = ctx.new_page()
        _login_if_needed(page)
        ctx.storage_state(path=str(STATE))          # reuse the session tomorrow
        for a in accounts:
            try:
                # TODO: open this account's bill page (URL, search box or dropdown)
                page.goto(f"TODO_BILL_PAGE_URL_FOR_{a['account_no']}")
                page.wait_for_load_state("networkidle")
                (day_dir / f"{a['account_no']}.html").write_text(page.content(), encoding="utf-8")
            except Exception as e:                    # no file saved: run() reports it as failed
                print(f"fetch failed for {a['account_no']}: {e}")
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(day_dir / f"{a['account_no']}.error.png"))
            page.wait_for_timeout(1500)               # be gentle with the portal
        browser.close()


def _money(text):
    m = re.search(r"([\d,]+\.\d{2})", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    amount = _money(soup.select_one("TODO-amount").get_text())
    outstanding = _money(soup.select_one("TODO-outstanding").get_text())
    due = dt.datetime.strptime(soup.select_one("TODO-due-date").get_text(strip=True),
                               "%d/%m/%Y").date()
    return amount, outstanding, due


def run():
    if not os.environ.get("SEB_LOGIN_URL"):
        return                                        # not configured yet
    with job_run("electric_check") as s:
        with connect() as c:
            accounts = c.execute("""SELECT id, account_no FROM utility_accounts
                                    WHERE type = 'electric' ORDER BY id""").fetchall()
        day_dir = RAW / dt.date.today().isoformat()
        fetch(accounts, day_dir)
        failed = []
        with connect() as c:
            for a in accounts:
                f = day_dir / f"{a['account_no']}.html"
                try:
                    amount, outstanding, due = parse(f.read_text(encoding="utf-8"))
                except Exception as e:
                    failed.append(f"{a['account_no']}: {e}")
                    continue
                c.execute("""INSERT INTO bill_checks (utility_account_id, amount_due, outstanding,
                                                     due_date, source, raw_ref)
                             VALUES (%s, %s, %s, %s, 'seb_portal', %s)""",
                          (a["id"], amount, outstanding, due, str(f)))
                s["rows"] += 1
        if failed:
            raise RuntimeError(f"{len(failed)} account(s) failed:\n" + "\n".join(failed[:20]))


if __name__ == "__main__":       # offline test: python -m jobs.electric raw/electric/2026-10-08
    for f in sorted(pathlib.Path(sys.argv[1]).glob("*.html")):
        print(f.name, parse(f.read_text(encoding="utf-8")))
