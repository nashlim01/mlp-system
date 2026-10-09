"""Daily Sarawak Energy check: logs in to SEBCares with the company login, finds every subscribed
account and keeps the bill data each account's page loads (the portal's own JSON, not the HTML).

  python -m jobs.electric                      # normal run (what the scheduler does)
  python -m jobs.electric --headed             # same, with a visible browser
  python -m jobs.electric --login-manually     # visible browser, you type the login yourself
  python -m jobs.electric --offline raw/electric/2026-10-09   # re-read saved JSON, no browser
Run from the worker/ folder.
"""
import datetime as dt
import json
import os
import pathlib
import sys
import time

from common import connect, job_run
from jobs.seb_parse import parse_accounts, parse_bill, total_rows

BASE = (os.environ.get("SEB_LOGIN_URL") or "https://sebcares.sarawakenergy.com/SEBCares/").split("/SEBCares")[0] \
    + "/SEBCares/"
RAW = pathlib.Path("raw/electric")
STATE = pathlib.Path("state/seb_state.json")


class Capture:
    """Keeps the JSON of every SEBCares data request the page makes."""

    def __init__(self, page):
        self.items = []
        page.on("response", self._on)

    def _on(self, r):
        if "/screenservices/" in r.url and r.ok:
            try:
                self.items.append((r.url, r.json().get("data")))
            except Exception:
                pass

    def clear(self):
        self.items.clear()

    def wait(self, page, test, seconds, stop=None):
        """The first captured data passing test(url, data); None on timeout or once stop() is true."""
        end = time.time() + seconds
        while time.time() < end:
            for url, data in list(self.items):
                if data is not None and test(url, data):
                    return data
            if stop and stop():
                return None
            page.wait_for_timeout(300)
        return None


def _accounts_loaded(url, data):
    return "GetContractAccounts" in url                    # first page and each "load more" page


def _on_portal(page):
    """Logged in = on a SEBCares page that isn't the login or splash screen (Google/SarawakPass pages don't count)."""
    u = page.url
    return u.startswith(BASE) and "/Login" not in u and "Splash" not in u


def _wait_logged_in(page, seconds, why):
    """The portal changes pages without reloading, so watch the address itself."""
    end = time.time() + seconds
    while not _on_portal(page):
        if time.time() > end:
            raise RuntimeError(why)
        page.wait_for_timeout(500)
    page.wait_for_timeout(3000)                              # let the portal finish setting up the session


def _login(page):
    page.goto(BASE + "Login")
    page.locator("#Input_UsernameVal").fill(os.environ["SEB_USERNAME"])
    page.locator("#Input_PasswordVal").fill(os.environ["SEB_PASSWORD"])
    page.get_by_role("button", name="Login", exact=True).click()
    _wait_logged_in(page, 30, "SEBCares login failed (wrong email/password, or the page asked for more)")


def _load_accounts(page, cap):
    """Open My Account the way a person would (menu link), falling back to the address."""
    cap.clear()
    link = page.get_by_text("Account", exact=True)
    try:
        if link.count():
            link.first.click()
        else:
            page.goto(BASE + "Accounts")
    except Exception:
        page.goto(BASE + "Accounts")
    return cap.wait(page, _accounts_loaded, 45, stop=lambda: "/Login" in page.url)


def _open_accounts(page, cap, manual):
    cap.clear()
    page.goto(BASE + "Accounts")
    data = cap.wait(page, _accounts_loaded, 30, stop=lambda: "/Login" in page.url)
    if data is not None:
        return data
    if not manual:
        _login(page)
        data = _load_accounts(page, cap)
        if data is None:
            raise RuntimeError(f"SEBCares account list did not load (page: {page.url})")
        return data
    end = time.time() + 600
    print("Log in to SEBCares in the browser window (you have 10 minutes)...", flush=True)
    while time.time() < end:
        _wait_logged_in(page, end - time.time(), "Nobody logged in within 10 minutes")
        print(f"Logged in (now at {page.url}); opening the account list...", flush=True)
        data = _load_accounts(page, cap)
        if data is not None:
            return data
        print(f"The account list didn't load (page: {page.url}). If you're back on Login, please log in again.",
              flush=True)
        if _on_portal(page):                                 # still logged in but no list: try the address once
            cap.clear()
            page.goto(BASE + "Accounts")
            data = cap.wait(page, _accounts_loaded, 45, stop=lambda: "/Login" in page.url)
            if data is not None:
                return data
    raise RuntimeError("SEBCares account list did not load within 10 minutes")


_SCROLL_DOWN = """() => { window.scrollTo(0, document.body.scrollHeight);
  for (const el of document.querySelectorAll('*'))
    if (el.scrollHeight > el.clientHeight + 50) el.scrollTop = el.scrollHeight; }"""


def _all_accounts(page, cap, first):
    """The portal lists 20 accounts and loads 20 more each time the list is scrolled to the end.
    Scroll until every account the portal reports (TotalRows) is in. Returns (pages, accounts, total)."""
    pages = [first]
    accounts, total = parse_accounts(pages), total_rows(pages)
    stuck = 0
    while total and len(accounts) < total and stuck < 3:
        seen = len(cap.items)
        page.evaluate(_SCROLL_DOWN)
        page.mouse.wheel(0, 4000)
        end = time.time() + 10
        while len(cap.items) == seen and time.time() < end:
            page.wait_for_timeout(300)
        pages = [d for u, d in cap.items if d is not None and _accounts_loaded(u, d)] or pages
        found = parse_accounts(pages)
        stuck = stuck + 1 if len(found) == len(accounts) else 0
        accounts = found
        total = max(total, total_rows(pages) or 0)
        print(f"  account list: {len(accounts)} of {total} loaded", flush=True)
    return pages, accounts, total


def _bill(page, cap, a, manual):
    """One account's bill data, logging in again if the session ran out during a long run."""
    for attempt in (1, 2):
        cap.clear()
        page.goto(f"{BASE}BillInformation?UserNotificationId=0"
                  f"&ContractSubscriptionId={a['subscription_id']}&ContractAccountNumber={a['account_no']}")
        data = cap.wait(page, lambda u, d: isinstance(d, dict) and "CustomerInformations" in d, 45,
                        stop=lambda: "/Login" in page.url)
        if data is not None:
            return data
        if "/Login" in page.url and attempt == 1:
            print("  session ended: logging in again", flush=True)
            if manual:
                print("  please log in again in the browser window", flush=True)
                _wait_logged_in(page, 600, "Nobody logged in again within 10 minutes")
            else:
                _login(page)
            continue
        break
    raise RuntimeError("bill data did not load" + (" (logged out)" if "/Login" in page.url else ""))


def fetch(day_dir, headed=False, manual=False, only=None):
    """Saves accounts.json and <account_no>.json in day_dir. Returns (accounts, failures)."""
    from playwright.sync_api import sync_playwright

    day_dir.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not (headed or manual))
        ctx = browser.new_context(storage_state=str(STATE) if STATE.exists() else None,
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        cap = Capture(page)
        first = _open_accounts(page, cap, manual)
        ctx.storage_state(path=str(STATE))                 # reuse the session next time if still valid
        pages, accounts, total = _all_accounts(page, cap, first)
        (day_dir / "accounts.json").write_text(json.dumps(pages, indent=1), encoding="utf-8")
        if total and len(accounts) < total:
            failed.append(f"account list: only {len(accounts)} of {total} accounts could be loaded")
        if only:
            accounts = [a for a in accounts if a["account_no"] in only]
        print(f"{len(accounts)} subscribed account(s) found", flush=True)
        started = time.time()
        for i, a in enumerate(accounts, 1):
            try:
                data = _bill(page, cap, a, manual)
                (day_dir / f"{a['account_no']}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
                print(f"  {i}/{len(accounts)} {a['account_no']} ({a['nickname']}): ok", flush=True)
            except Exception as e:
                failed.append(f"{a['account_no']}: {e}")
                print(f"  {i}/{len(accounts)} {a['account_no']}: FAILED {e}", flush=True)
                try:
                    page.screenshot(path=str(day_dir / f"{a['account_no']}.error.png"))
                except Exception:
                    pass
            page.wait_for_timeout(1500)                       # be gentle with the portal
        if accounts:
            print(f"{len(accounts)} account(s) in {time.time() - started:.0f}s", flush=True)
        browser.close()
    return accounts, failed


def read_saved(day_dir):
    """Parsed bill data for every <account_no>.json saved in day_dir."""
    out = []
    for f in sorted(day_dir.glob("*.json")):
        if f.name != "accounts.json":
            out.append((f, parse_bill(json.loads(f.read_text(encoding="utf-8")))))
    return out


def save(c, info, raw_ref):
    """One account's parsed data -> utility_accounts (found or added, unit left for staff), bill_checks, utility_bills."""
    ua = c.execute("""
        INSERT INTO utility_accounts (type, account_no, nickname, address, subscription_id, tariff, active, last_seen_at)
        VALUES ('electric', %(account_no)s, %(nickname)s, %(address)s, %(subscription_id)s, %(tariff)s, %(active)s, NOW())
        ON CONFLICT (type, account_no) DO UPDATE SET
          nickname = EXCLUDED.nickname, address = COALESCE(EXCLUDED.address, utility_accounts.address),
          subscription_id = EXCLUDED.subscription_id, tariff = EXCLUDED.tariff,
          active = EXCLUDED.active, last_seen_at = NOW()
        RETURNING id""", info).fetchone()["id"]
    c.execute("""INSERT INTO bill_checks (utility_account_id, amount_due, outstanding, due_date, deposit, credit,
                                         source, raw_ref)
                 VALUES (%s, %s, %s, %s, %s, %s, 'seb_portal', %s)""",
              (ua, info["amount_due"], info["outstanding"], info["due_date"], info["deposit"], info["credit"],
               raw_ref))
    for b in info["bills"]:
        if not b["bill_date"] or b["amount"] is None:
            continue
        c.execute("""INSERT INTO utility_bills (utility_account_id, bill_no, bill_date, due_date, period_start,
                                               period_end, amount, total_due, pdf_name, reversed)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                     ON CONFLICT (utility_account_id, bill_no) DO UPDATE SET reversed = EXCLUDED.reversed,
                       amount = EXCLUDED.amount, total_due = EXCLUDED.total_due, due_date = EXCLUDED.due_date""",
                  (ua, b["bill_no"], b["bill_date"], b["due_date"], b["period_start"], b["period_end"],
                   b["amount"], b["total_due"], b["pdf_name"], b["reversed"]))
    return ua


def run(headed=False, manual=False):
    if not manual and not os.environ.get("SEB_USERNAME"):
        return                                                # not configured yet
    with job_run("electric_check") as s:
        day_dir = RAW / dt.date.today().isoformat()
        _, failed = fetch(day_dir, headed=headed, manual=manual)
        with connect() as c:
            for f, info in read_saved(day_dir):
                save(c, info, str(f))
                s["rows"] += 1
            c.execute("SELECT attach_utility_bills()")         # new bills -> the tenants' monthly totals
        if failed:
            raise RuntimeError(f"{len(failed)} account(s) failed:\n" + "\n".join(failed[:20]))


def _print(info):
    print(f"{info['account_no']}  {info['nickname'] or ''}  {info['address'] or ''}")
    print(f"   outstanding RM{info['outstanding']}  latest bill RM{info['amount_due']} due {info['due_date']}"
          f"  deposit RM{info['deposit']}  tariff {info['tariff']}  active {info['active']}")
    for b in info["bills"][:3]:
        print(f"   bill {b['bill_no']}  {b['bill_date']}  {b['period_start']}..{b['period_end']}"
              f"  RM{b['amount']} (total RM{b['total_due']})  due {b['due_date']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--offline"]:
        for _, info in read_saved(pathlib.Path(args[1])):
            _print(info)
    else:
        run(headed="--headed" in args, manual="--login-manually" in args)
