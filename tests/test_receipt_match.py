"""Rules for matching transfer slips (portal/receipt_match.py).  Run: python -m pytest tests/"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "portal"))
from receipt_match import allocate, bank_pick, decide, name_in_text, norm_alias, payer_text, reference_hit  # noqa: E402

TODAY = dt.date(2026, 10, 8)
SLIP = {"is_payment_slip": True, "successful": True, "amount": 10.0, "paid_at": "2026-10-08T18:15",
        "sender_name": None, "recipient_acct_last4": "6814", "reference": "Rental Demo",
        "txn_ref": "2610086789453521", "confidence": 0.97}
CREDIT = {"id": 1, "txn_date": dt.date(2026, 10, 8), "amount": 10.0, "status": "unmatched",
          "description": "- LIM ZHI HAO * Rental Demo", "matched_payment_id": None}
OTHER = {"id": 2, "txn_date": dt.date(2026, 10, 8), "amount": 1040.0, "status": "unmatched",
         "description": "MBB CT- PHUAN QIU YAN * Fund Transfer", "matched_payment_id": None}
NAMES = ["Lim Zhi Hao"]                                   # the tenant the slip was uploaded for
OCT = {"period": dt.date(2026, 10, 1), "balance": 10.0}
CO = {"6814"}


def run(slip=None, credits=None, names=None, line=OCT, **kw):
    return decide(slip or SLIP, [CREDIT, OTHER] if credits is None else credits,
                  NAMES if names is None else names, line, CO, TODAY, dt.date(2026, 10, 8), **kw)


# ── names ────────────────────────────────────────────────────────────────────
def test_exact_name_in_bank_line():
    assert name_in_text("Lim Zhi Hao", "- LIM ZHI HAO * Rental Demo")             # case and symbols ignored
    assert name_in_text("Phuan Qiu Yan", "MBB CT- PHUAN QIU YAN * Fund Transfer")  # bank prefix before the name
    assert name_in_text("Sumerni Binti Roslan", "IBG SUMERNI BINTI ROSLAN")


def test_name_cut_off_by_the_bank_matches_up_to_the_cut():
    name = "Nurul Asyikin Binti Jeffrey"
    assert name_in_text(name, "IBG NURUL ASYIKIN BINTI JEF * Rent")             # cut inside a word
    assert name_in_text(name, "IBG NURUL ASYIKIN BINTI")                        # cut on a word boundary
    assert not name_in_text(name, "IBG NURUL ASYIKIN BINTI JEX")                # visible part differs
    assert not name_in_text("Lim Zhi Hao", "IBG LIM ZHI")                       # too little visible to tell


def test_strict_no_fuzzy_no_abbreviation_no_extra_words():
    assert not name_in_text("Sumerni Binti Roslan", "MBB CT- SUMERNI BT ROSLAN")   # BT is not BINTI
    assert not name_in_text("Nurul Asyikin Binti Jeffrey", "NURUL ASYIKIN BINTI JEFFERY")  # spelling
    assert not name_in_text("Nurul Asyikin Binti Jeffrey", "NURUL ASYIKIN JEFFREY")  # word left out
    assert not name_in_text("Lim Zhi Hao", "LIM ZHI HAO ENTERPRISE * Rent")     # someone else (a company)
    assert not name_in_text("Lim Zhi Hao", "HAO ZHI LIM")                       # order matters
    assert not name_in_text("Lim Zhi Hao", "LIM AH KOW")
    assert not name_in_text("Lim Zhi Hao", "MBB CT- PHUAN QIU YAN * Fund Transfer")


def test_alias_normalisation_and_reference():
    assert norm_alias("Ahmad  bin Ali.") == "AHMAD BIN ALI"                        # every word kept
    assert payer_text("MBB CT- PHUAN QIU YAN * Fund Transfer") == "PHUAN QIU YAN"   # only to pre-fill
    assert reference_hit("Rental Demo", "- LIM ZHI HAO * Rental Demo")
    assert not reference_hit("Rental Demo", "MBB CT- PHUAN QIU YAN * Fund Transfer")


# ── the happy path from the demo files ───────────────────────────────────────
def test_demo_slip_tallies_with_bank_name_and_amount():
    d = run()
    assert d.status == "matched", d.flags
    assert d.bank_txn_id == 1


def test_partial_payment_is_recorded():
    d = run(line={"period": dt.date(2026, 10, 1), "balance": 850.0})
    assert d.status == "matched", d.flags                 # shows as Partly paid on the Rent Board


def test_already_recorded_payment_is_linked_not_duplicated():
    paid = {**CREDIT, "status": "matched", "matched_payment_id": 77}
    d = run(credits=[paid], line={"period": dt.date(2026, 10, 1), "balance": 0.0})
    assert d.status == "matched" and d.link_payment_id == 77


# ── everything that must go to review ────────────────────────────────────────
def test_not_in_bank_statement():
    d = run(credits=[OTHER])
    assert d.status == "review" and any("No RM10.00 payment in the bank statement" in f for f in d.flags)


def test_credit_outside_date_window_is_not_used():
    late = {**CREDIT, "txn_date": dt.date(2026, 10, 13)}
    assert run(credits=[late]).status == "review"


def test_amount_in_bank_but_under_another_name_until_saved():
    d = run(names=["Siti Aminah"])
    assert d.status == "review" and any("isn't this tenant's name" in f for f in d.flags)
    assert d.bank_txn_id == 1                             # suggested for staff
    assert run(names=["Siti Aminah", "LIM ZHI HAO"]).status == "matched"   # after staff saved the name


def test_more_than_owed_or_already_paid():
    assert any("more than what's owed" in f for f in run(line={"period": dt.date(2026, 10, 1), "balance": 5.0}).flags)
    assert any("already fully paid" in f for f in run(line={"period": dt.date(2026, 10, 1), "balance": 0.0}).flags)


def test_two_payments_under_the_name_need_a_pick():
    a = {**CREDIT, "id": 3, "description": "IBG LIM ZHI HAO"}
    b = {**a, "id": 4}
    d = run(credits=[a, b], slip={**SLIP, "reference": None})
    assert d.status == "review" and any("pick the right one" in f for f in d.flags)


def test_reference_breaks_a_tie():
    plain = {**CREDIT, "id": 9, "description": "IBG LIM ZHI HAO"}
    d = run(credits=[plain, CREDIT])
    assert d.status == "matched" and d.bank_txn_id == 1


def test_wrong_account_low_confidence_future_and_failed():
    assert any("not the company account" in f for f in run(slip={**SLIP, "recipient_acct_last4": "1234"}).flags)
    assert run(slip={**SLIP, "confidence": 0.5}).status == "review"
    assert run(slip={**SLIP, "paid_at": "2026-12-01T10:00"}).status == "review"
    assert run(slip={**SLIP, "successful": False}).status == "review"


def test_not_a_slip():
    assert run(slip={**SLIP, "is_payment_slip": False}).status == "not_slip"


# ── money in the bank with no slip ───────────────────────────────────────────
SEPT = {"schedule_id": 1, "period": dt.date(2026, 9, 1), "due_date": dt.date(2026, 9, 1), "balance": 0.0}
OCT_LINE = {"schedule_id": 2, "period": dt.date(2026, 10, 1), "due_date": dt.date(2026, 10, 1), "balance": 950.0}
NOV_LINE = {"schedule_id": 3, "period": dt.date(2026, 11, 1), "due_date": dt.date(2026, 11, 1), "balance": 950.0}
SITI = {"tenancy_id": 2, "names": ["Siti Aminah"], "lines": [SEPT, OCT_LINE, NOV_LINE]}
LIM = {"tenancy_id": 1, "names": ["Lim Zhi Hao"], "lines": [{**OCT_LINE, "schedule_id": 9, "balance": 10.0}]}


def credit(desc, amount, day=dt.date(2026, 10, 8)):
    return {"id": 5, "txn_date": day, "amount": amount, "description": desc}


def test_bank_credit_under_tenant_name_is_recorded_for_oldest_unpaid_month():
    p = bank_pick(credit("IBG SITI AMINAH", 950.0), [LIM, SITI])
    assert p.auto and p.tenancy_id == 2 and p.line["schedule_id"] == 2


def test_bank_part_payment_is_recorded_and_leaves_remainder():
    p = bank_pick(credit("IBG SITI AMINAH", 500.0), [SITI])
    assert p.auto and p.line["schedule_id"] == 2               # RM450 stays owing for October


def test_bank_credit_more_than_owed_or_unclear_waits_for_staff():
    p = bank_pick(credit("IBG SITI AMINAH", 1900.0), [SITI])
    assert not p.auto and p.tenancy_id == 2 and "more than" in p.reason
    assert not bank_pick(credit("IBG AHMAD BIN ALI", 950.0), [SITI]).tenancy_id       # nobody's name
    twin = {**SITI, "tenancy_id": 7}
    assert not bank_pick(credit("IBG SITI AMINAH", 950.0), [SITI, twin]).auto        # two tenants, same name


def test_bank_credit_too_early_for_any_unpaid_month():
    only_nov = {**SITI, "lines": [NOV_LINE]}
    assert not bank_pick(credit("IBG SITI AMINAH", 950.0, dt.date(2026, 8, 1)), [only_nov]).auto
    assert bank_pick(credit("IBG SITI AMINAH", 950.0, dt.date(2026, 10, 28)), [only_nov]).auto  # paid in advance


def test_allocate_oldest_first_and_extra_on_last_month():
    lines = [SEPT, OCT_LINE, NOV_LINE]
    assert [(ln["schedule_id"], a) for ln, a in allocate(1400, lines)] == [(2, 950.0), (3, 450.0)]
    assert [(ln["schedule_id"], a) for ln, a in allocate(2000, lines)] == [(2, 950.0), (3, 1050.0)]
    assert [(ln["schedule_id"], a) for ln, a in allocate(300, lines)] == [(2, 300.0)]
