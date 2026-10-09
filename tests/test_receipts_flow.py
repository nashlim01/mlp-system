"""End-to-end receipt flow on a THROWAWAY database, with the AI replaced by fixed answers.

  MLP_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/mlp_test python -m pytest tests/test_receipts_flow.py
Drops and recreates the public schema. Never point it at Supabase.
"""
import datetime as dt
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("MLP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL or "supabase" in (URL or ""), reason="needs a throwaway MLP_TEST_DATABASE_URL")
sys.path.insert(0, str(ROOT / "portal"))

import receipts_core as rc  # noqa: E402
from receipt_ai import SlipData  # noqa: E402

TODAY = dt.date(2026, 10, 8)
DEMO = dict(is_payment_slip=True, successful=True, amount=10.0, currency="MYR", paid_at="2026-10-08T18:15",
            sender_name=None, sender_bank="UOB", recipient_name="LIM ZHI HAO", recipient_bank="Maybank Berhad",
            recipient_acct_last4="6814", transfer_mode="DuitNow (Pay-to-Account)", reference="Rental Demo",
            txn_ref="2610086789453521", duitnow_ref="20261008UOVBMYKL010ORM09258988", confidence=0.97, notes=None)


def reader(**over):
    return lambda data, media: SlipData(**{**DEMO, **over})


def store(path, data, media):
    return path


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setattr(rc, "today_myt", lambda: TODAY)
    monkeypatch.setenv("COMPANY_ACCOUNT_LAST4", "6814")
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in sorted((ROOT / "db/migrations").glob("*.sql")):
            c.execute(f.read_text())
        c.execute("""
            INSERT INTO staff (id, name, email, role) VALUES (1, 'Admin', 'a@x.test', 'admin');
            INSERT INTO landlords (id, code, name) VALUES (1, 'L1', 'Owner');
            INSERT INTO units (id, code, address, landlord_id, assigned_staff_id) VALUES
              (1, 'U1', 'Unit 1', 1, 1), (2, 'U2', 'Unit 2', 1, 1);
            INSERT INTO tenants (id, name, phone) VALUES (1, 'Lim Zhi Hao', '60100000101'),
                                                     (2, 'Siti Aminah', '60100000102');
            INSERT INTO tenancies (id, unit_id, tenant_id, start_date, monthly_rent, due_day, status) VALUES
              (1, 1, 1, '2026-09-01', 10, 1, 'active'), (2, 2, 2, '2026-09-01', 950, 1, 'active');
            SELECT generate_rent_schedule('2026-10-01'); SELECT generate_rent_schedule('2026-11-01');""")
        rc.import_bank_credits(c, [
            {"txn_date": TODAY, "description": "- LIM ZHI HAO * Rental Demo", "amount": 10.0},
            {"txn_date": TODAY, "description": "MBB CT- PHUAN QIU YAN * Fund Transfer", "amount": 1040.0}],
            "Company Maybank", 1, "screenshot", "test")
        yield c


def one(c, sql, *p):
    return c.execute(sql, p).fetchone()


def line(c, tenancy, period="2026-10-01"):
    return one(c, "SELECT schedule_id FROM v_rent_status WHERE tenancy_id = %s AND period = %s",
               tenancy, period)["schedule_id"]


def upload(c, data, name, tenancy=1, period="2026-10-01", **kw):
    return rc.process_slip(c, data, name, 1, tenancy_id=tenancy, schedule_id=line(c, tenancy, period),
                           store=store, **kw)


def test_demo_slip_is_auto_recorded_then_duplicates_are_caught(conn):
    res = upload(conn, b"sender-jpeg", "Sender.jpeg", reader=reader())
    assert res["status"] == "matched", res["flags"]
    r = one(conn, "SELECT * FROM receipts WHERE id = %s", res["receipt_id"])
    assert r["created_payment"] and r["tenancy_id"] == 1
    assert one(conn, "SELECT status FROM v_rent_status WHERE tenancy_id = 1 AND period = '2026-10-01'")["status"] == "PAID"
    assert one(conn, "SELECT status FROM bank_transactions WHERE amount = 10")["status"] == "matched"
    assert one(conn, "SELECT receipt_path, method FROM payments")["method"] == "duitnow"
    # same file again -> nothing new
    again = upload(conn, b"sender-jpeg", "Sender.jpeg", reader=reader())
    assert again["status"] == "duplicate" and one(conn, "SELECT count(*) AS n FROM receipts")["n"] == 1
    # different screenshot of the same transfer -> duplicate row, no second payment
    shot = upload(conn, b"another-screenshot", "copy.png", reader=reader())
    assert shot["status"] == "duplicate"
    assert one(conn, "SELECT count(*) AS n FROM payments WHERE NOT voided")["n"] == 1


def test_no_bank_credit_yet_then_rematch_after_upload(conn):
    conn.execute("DELETE FROM bank_transactions")
    res = upload(conn, b"x", "s.jpg", reader=reader())
    assert res["status"] == "review" and any("No RM10.00 payment in the bank statement" in f for f in res["flags"])
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "- LIM ZHI HAO * Rental Demo", "amount": 10.0}],
                           "Company Maybank", 1, "screenshot", "t2")
    assert rc.rematch_pending(conn, 1)["matched"] == 1


def test_undo_voids_and_never_auto_records_again(conn):
    rid = upload(conn, b"x", "s.jpg", reader=reader())["receipt_id"]
    assert rc.undo_receipt(conn, rid, 1)
    p = one(conn, "SELECT voided, void_reason FROM payments")
    assert p["voided"] and p["void_reason"] == "Receipt match undone"
    assert one(conn, "SELECT status FROM bank_transactions WHERE amount = 10")["status"] == "unmatched"
    assert rc.rematch_pending(conn, 1) == {"matched": 0, "review": 1}
    assert one(conn, "SELECT no_auto FROM receipts WHERE id = %s", rid)["no_auto"]


def test_other_payer_goes_to_review_and_name_is_saved(conn):
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG AHMAD BIN ALI", "amount": 950.0}],
                           "Company Maybank", 1, "csv", "t3")
    slip = reader(amount=950.0, reference=None, txn_ref="REF-950-OCT", recipient_name="MLP")
    res = upload(conn, b"siti-oct", "siti.jpg", tenancy=2, reader=slip)          # uploaded for Siti
    assert res["status"] == "review" and any("isn't this tenant's name" in f for f in res["flags"])
    r = one(conn, "SELECT bank_txn_id FROM receipts WHERE id = %s", res["receipt_id"])
    msg = rc.confirm_receipt(conn, res["receipt_id"], 1, bank_txn_id=r["bank_txn_id"], remember_payer="AHMAD BIN ALI")
    assert msg == "Payment recorded."
    assert one(conn, "SELECT alias_norm FROM payer_aliases")["alias_norm"] == "AHMAD BIN ALI"
    assert one(conn, "SELECT status FROM v_rent_status WHERE tenancy_id = 2 AND period = '2026-10-01'")["status"] == "PAID"
    with pytest.raises(ValueError):                         # second click: already handled
        rc.confirm_receipt(conn, res["receipt_id"], 1)
    # next month, same payer -> checked automatically through the saved name
    rc.import_bank_credits(conn, [{"txn_date": dt.date(2026, 11, 2), "description": "IBG AHMAD BIN ALI",
                                   "amount": 950.0}], "Company Maybank", 1, "csv", "t4")
    rc.today_myt = lambda: dt.date(2026, 11, 2)
    nov = upload(conn, b"siti-nov", "siti2.jpg", tenancy=2, period="2026-11-01",
                 reader=reader(amount=950.0, reference=None, txn_ref="REF-950-NOV", paid_at="2026-11-02T09:00"))
    assert nov["status"] == "matched", nov["flags"]


def test_partial_payment_is_recorded_with_balance_left(conn):
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 500.0}],
                           "Company Maybank", 1, "csv", "t5")
    res = upload(conn, b"siti-part", "p.jpg", tenancy=2,
                 reader=reader(amount=500.0, reference=None, txn_ref="REF-500"))
    assert res["status"] == "matched", res["flags"]
    r = one(conn, "SELECT paid, balance FROM v_rent_status WHERE tenancy_id = 2 AND period = '2026-10-01'")
    assert (r["paid"], r["balance"]) == (500, 450)          # Partly paid (Overdue once past the due date)


def test_staff_recorded_payment_is_linked_not_doubled(conn):
    sched = one(conn, "SELECT schedule_id FROM v_rent_status WHERE tenancy_id = 1 AND period = '2026-10-01'")
    pid = one(conn, """INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date, recorded_by)
                       VALUES (1, %s, 10, '2026-10-08', 1) RETURNING id""", sched["schedule_id"])["id"]
    conn.execute("UPDATE bank_transactions SET status = 'matched', matched_payment_id = %s WHERE amount = 10", (pid,))
    res = upload(conn, b"x", "s.jpg", reader=reader())
    assert res["status"] == "matched", res["flags"]
    assert one(conn, "SELECT count(*) AS n FROM payments")["n"] == 1
    assert one(conn, "SELECT receipt_path FROM payments")["receipt_path"].startswith("slips/")
    assert one(conn, "SELECT created_payment FROM receipts")["created_payment"] is False


# ── money in the bank with no slip, grace period, reminder stages ────────────
def test_bank_credit_without_slip_records_part_payment_and_asks_for_remainder(conn):
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 500.0}],
                           "Company Maybank", 1, "csv", "nb1")
    conn.execute("UPDATE rent_schedule SET due_date = today_myt() - 1 WHERE id = %s", (line(conn, 2),))  # in grace
    conn.execute("""INSERT INTO reminder_log (tenancy_id, rent_schedule_id, staff_id, template_code, created_at)
                    VALUES (2, %s, 1, 'rent_reminder', NOW() - INTERVAL '1 day')""", (line(conn, 2),))
    assert rc.auto_from_bank(conn, 1) == 2                   # Siti's RM500 and Lim's demo RM10
    p = one(conn, "SELECT amount, auto_from_bank, bank_txn_id FROM payments WHERE tenancy_id = 2")
    assert p["amount"] == 500 and p["auto_from_bank"] and p["bank_txn_id"]
    r = one(conn, "SELECT balance, stage FROM v_reminder_due WHERE schedule_id = %s", line(conn, 2))
    assert r["balance"] == 450 and r["stage"] == "rent_balance"            # remind again with the remainder
    assert rc.auto_from_bank(conn, 1) == 0                                  # not twice


def test_undo_bank_auto_and_staff_records_it_instead(conn):
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 950.0}],
                           "Company Maybank", 1, "csv", "nb2")
    rc.auto_from_bank(conn, 1)
    pid = one(conn, "SELECT id FROM payments WHERE tenancy_id = 2")["id"]
    assert rc.undo_bank_payment(conn, pid, 1)
    b = one(conn, "SELECT id, status, no_auto FROM bank_transactions WHERE amount = 950")
    assert b["status"] == "unmatched" and b["no_auto"]
    assert rc.auto_from_bank(conn, 1) == 0                                  # undone: never automatic again
    hints = rc.bank_hints(conn)[2]
    assert hints[0][0]["id"] == b["id"]                                     # still offered to staff
    assert rc.record_bank_credit(conn, b["id"], 2, 1)
    assert one(conn, "SELECT status FROM v_rent_status WHERE schedule_id = %s", line(conn, 2))["status"] == "PAID"


def test_slip_after_bank_auto_links_to_the_same_payment(conn):
    assert rc.auto_from_bank(conn, 1) == 1                    # the demo credit "- LIM ZHI HAO * Rental Demo"
    res = upload(conn, b"x", "s.jpg", reader=reader())
    assert res["status"] == "matched"
    assert one(conn, "SELECT count(*) AS n FROM payments WHERE NOT voided AND tenancy_id = 1")["n"] == 1


def test_grace_period_and_reminder_stages(conn):
    sid = line(conn, 2)
    def at(due, grace=7):
        conn.execute("UPDATE rent_schedule SET due_date = %s WHERE id = %s", (due, sid))
        conn.execute("UPDATE tenancies SET grace_days = %s WHERE id = 2", (grace,))
        return one(conn, "SELECT status, stage FROM v_reminder_due WHERE schedule_id = %s", sid)
    today = one(conn, "SELECT today_myt() AS d")["d"]
    d = dt.timedelta
    assert at(today + d(days=1)) == {"status": "DUE", "stage": None}                 # not before the due date
    assert at(today) == {"status": "DUE", "stage": "rent_reminder"}                  # 1st: on the due date
    conn.execute("INSERT INTO reminder_log (tenancy_id, rent_schedule_id, staff_id, template_code) "
                 "VALUES (2, %s, 1, 'rent_reminder')", (sid,))
    assert at(today - d(days=3)) == {"status": "GRACE", "stage": None}               # day 3 of grace: nothing
    assert at(today - d(days=5)) == {"status": "GRACE", "stage": "rent_grace_reminder"}  # 2nd: day 5 of 7
    conn.execute("INSERT INTO reminder_log (tenancy_id, rent_schedule_id, staff_id, template_code) "
                 "VALUES (2, %s, 1, 'rent_grace_reminder')", (sid,))
    assert at(today - d(days=6)) == {"status": "GRACE", "stage": None}
    assert at(today - d(days=7)) == {"status": "GRACE", "stage": "rent_grace_end"}   # 3rd: last day of grace
    conn.execute("INSERT INTO reminder_log (tenancy_id, rent_schedule_id, staff_id, template_code) "
                 "VALUES (2, %s, 1, 'rent_grace_end')", (sid,))
    assert at(today - d(days=8)) == {"status": "OVERDUE", "stage": None}             # overdue: staff settle in person
    assert at(today - d(days=30)) == {"status": "OVERDUE", "stage": None}
    assert at(today - d(days=3), grace=0)["status"] == "OVERDUE"                     # tenancy with no grace


def test_staff_records_a_two_month_credit_split_oldest_first(conn):
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 1900.0}],
                           "Company Maybank", 1, "csv", "nb3")
    assert rc.auto_from_bank(conn, 1) == 1                    # only Lim's RM10: RM1,900 is more than one month
    b = one(conn, "SELECT id FROM bank_transactions WHERE amount = 1900")
    assert rc.record_bank_credit(conn, b["id"], 2, 1)
    rows = conn.execute("SELECT period, paid, balance FROM v_rent_status WHERE tenancy_id = 2 ORDER BY period").fetchall()
    assert [(r["paid"], r["balance"]) for r in rows] == [(950, 0), (950, 0)]   # October and November
    pid = one(conn, "SELECT min(id) AS id FROM payments WHERE tenancy_id = 2")["id"]
    assert rc.undo_bank_payment(conn, pid, 1)                 # undo voids both halves
    assert one(conn, "SELECT count(*) AS n FROM payments WHERE tenancy_id = 2 AND NOT voided")["n"] == 0


# ── utilities in the monthly total ───────────────────────────────────────────
def test_utility_bills_join_the_right_month_and_rent_is_paid_first(conn):
    conn.execute("""INSERT INTO utility_accounts (id, unit_id, type, account_no) VALUES
                    (10, 2, 'electric', '100003716138'), (11, 2, 'water', 'W-778')""")
    conn.execute("""INSERT INTO utility_bills (utility_account_id, bill_no, bill_date, amount) VALUES
                    (10, 'SEB-SEP', '2026-09-22', 61.95),   -- newest bill before the 1 Oct due date
                    (10, 'SEB-OLD', '2025-10-21', 94.25),   -- history from the first scrape: never charged
                    (11, 'W-OCT',   '2026-10-05', 20.00)""")  # after 1 Oct, so it goes to November
    assert one(conn, "SELECT attach_utility_bills() AS n")["n"] == 2
    assert one(conn, "SELECT attach_utility_bills() AS n")["n"] == 0                  # each bill once
    oct_ = one(conn, "SELECT * FROM v_rent_status WHERE schedule_id = %s", line(conn, 2))
    nov = one(conn, "SELECT * FROM v_rent_status WHERE schedule_id = %s", line(conn, 2, "2026-11-01"))
    from decimal import Decimal as D
    assert (oct_["amount_due"], oct_["electric_due"], oct_["water_due"], oct_["total_due"]) == \
        (950, D("61.95"), 0, D("1011.95"))
    assert (nov["water_due"], nov["total_due"]) == (20, 970)
    conn.execute("INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date) VALUES (2, %s, 980, %s)",
                 (line(conn, 2), TODAY))
    r = one(conn, "SELECT * FROM v_rent_status WHERE schedule_id = %s", line(conn, 2))
    assert (r["rent_paid"], r["utilities_paid"], r["balance"]) == (950, 30, D("31.95")) and r["status"] != "PAID"


def test_bank_transfer_of_the_full_total_is_recorded(conn):
    conn.execute("INSERT INTO utility_accounts (id, unit_id, type, account_no) VALUES (10, 2, 'electric', '1')")
    conn.execute("INSERT INTO utility_bills (utility_account_id, bill_no, bill_date, amount) "
                 "VALUES (10, 'B1', '2026-09-22', 61.95)")
    conn.execute("SELECT attach_utility_bills()")
    rc.import_bank_credits(conn, [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 1011.95}],
                           "Company Maybank", 1, "csv", "u1")
    rc.auto_from_bank(conn, 1)
    assert one(conn, "SELECT status FROM v_rent_status WHERE schedule_id = %s", line(conn, 2))["status"] == "PAID"


# ── bank uploads: never read the same file twice ─────────────────────────────
def test_same_file_is_remembered_and_rereads_are_caught(conn):
    digest = rc.file_hash(b"screenshot-bytes")
    assert rc.find_upload(conn, digest) is None
    credits = [{"date": "2026-10-08", "description": "IBG SITI AMINAH", "amount": 950.0}]
    uid = rc.start_upload(conn, digest, "shot.png", "screenshot", "Company Maybank", 1, credits=credits)
    prev = rc.find_upload(conn, digest)
    assert prev["credits"] == credits and prev["added_at"] is None          # read, kept: no second AI call
    rows = [{"txn_date": TODAY, "description": "IBG SITI AMINAH", "amount": 950.0}]
    assert rc.import_bank_credits(conn, rows, "Company Maybank", 1, "screenshot", "b", uid) == 1
    prev = rc.find_upload(conn, digest)
    assert prev["added_at"] is not None and (prev["added"], prev["total"]) == (1, 1)
    # the same screenshot read again with a different 'taken on' date: dates shift by a day
    shifted = [{"txn_date": TODAY + dt.timedelta(days=1), "description": "IBG SITI AMINAH", "amount": 950.0},
               {"txn_date": TODAY, "description": "IBG SOMEONE ELSE", "amount": 950.0}]
    assert rc.looks_saved(conn, "Company Maybank", shifted) == [TODAY, None]
    log = one(conn, "SELECT how, file_name FROM v_bank_log WHERE description = 'IBG SITI AMINAH'")
    assert log["file_name"] == "shot.png" and log["how"] == "Not recorded yet"
    rc.auto_from_bank(conn, 1)
    log = one(conn, "SELECT how, paid_for FROM v_bank_log WHERE description = 'IBG SITI AMINAH'")
    assert log["how"].startswith("Auto") and "U2 · Siti Aminah · Oct 2026" in log["paid_for"]
