"""Receipt intake: store the slip, read it with AI, match it, auto-record or queue for review.

No Streamlit here: pass a psycopg connection (dict_row, autocommit) from the portal, or later from the
WhatsApp intake. Every decision is a conditional update and is written to audit_log.
"""
import datetime as dt
import hashlib
import json
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from bank import row_hash
from receipt_ai import media_type_for, read_slip
from receipt_match import Decision, allocate, bank_pick, decide, norm_alias, parse_paid_at
from storage import upload_file

TZ = ZoneInfo("Asia/Kuching")
UNDONE = "Match undone by staff: won't be auto-recorded again."


def today_myt() -> dt.date:
    return dt.datetime.now(TZ).date()


def company_last4() -> set:
    return {x.strip()[-4:] for x in os.environ.get("COMPANY_ACCOUNT_LAST4", "").split(",") if x.strip()}


def _audit(conn, staff_id, action, entity, entity_id, before=None, after=None):
    j = lambda v: None if v is None else Jsonb(v, dumps=lambda o: json.dumps(o, default=str))
    conn.execute("""INSERT INTO audit_log (staff_id, action, entity, entity_id, before, after)
                    VALUES (%s, %s, %s, %s, %s, %s)""", (staff_id, action, entity, entity_id, j(before), j(after)))


# ── context the rules need ───────────────────────────────────────────────────
def _context(conn, r):
    """Bank credits not yet claimed by another slip, the tenant's names, the rent month, bank coverage."""
    credits = conn.execute("""
        SELECT b.id, b.txn_date, b.amount, b.description, b.status, b.matched_payment_id
        FROM bank_transactions b
        WHERE b.txn_date >= CURRENT_DATE - 120
          AND NOT EXISTS (SELECT 1 FROM receipts x WHERE x.bank_txn_id = b.id
                          AND x.status IN ('matched', 'confirmed') AND x.id <> %s)""", (r["id"],)).fetchall()
    names = [row["n"] for row in conn.execute("""
        SELECT tn.name AS n FROM tenancies t JOIN tenants tn ON tn.id = t.tenant_id WHERE t.id = %s
        UNION ALL
        SELECT a.alias_norm FROM tenancies t JOIN payer_aliases a ON a.tenant_id = t.tenant_id WHERE t.id = %s""",
        (r["tenancy_id"], r["tenancy_id"])).fetchall()]
    line = conn.execute("SELECT period, balance FROM v_rent_status WHERE schedule_id = %s",
                        (r["rent_schedule_id"],)).fetchone() if r["rent_schedule_id"] else None
    up_to = conn.execute("SELECT max(txn_date) AS d FROM bank_transactions").fetchone()["d"]
    return credits, names, line, up_to


def _slip_dict(r) -> dict:
    return {"is_payment_slip": r["status"] != "not_slip", "successful": (r["ai_raw"] or {}).get("successful", True),
            "amount": float(r["amount"]) if r["amount"] is not None else None,
            "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None, "sender_name": r["sender_name"],
            "recipient_acct_last4": r["recipient_acct_last4"], "reference": r["reference"],
            "txn_ref": r["txn_ref"], "confidence": float(r["ai_confidence"] or 0)}


def _method(transfer_mode) -> str:
    return "duitnow" if "DUITNOW" in str(transfer_mode or "").upper() else "bank_transfer"


# ── intake ───────────────────────────────────────────────────────────────────
def process_slip(conn, data: bytes, filename: str, uploaded_by: int, *, tenancy_id: int, schedule_id=None,
                 source="upload", reader=read_slip, store=upload_file) -> dict:
    """Store + read + check one slip for a known tenant (and rent month). Returns
    {receipt_id, status, flags, message}. receipt_ai.AIError propagates (nothing is saved then)."""
    digest = hashlib.sha256(data).hexdigest()
    seen = conn.execute("SELECT id, status FROM receipts WHERE file_hash = %s", (digest,)).fetchone()
    if seen:
        return {"receipt_id": seen["id"], "status": "duplicate", "flags": [],
                "message": f"This exact file was already uploaded (slip #{seen['id']}, {seen['status']})."}

    media = media_type_for(filename)
    slip = reader(data, media)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    path = store(f"slips/{today_myt():%Y-%m}/{digest[:20]}.{ext}", data, media)

    dup = None
    if slip.txn_ref:
        dup = conn.execute("SELECT id FROM receipts WHERE txn_ref = %s AND status <> 'duplicate'",
                           (slip.txn_ref,)).fetchone()
    status = "duplicate" if dup else ("review" if slip.is_payment_slip else "not_slip")
    flags = [f"Same transfer as slip #{dup['id']} (bank ref {slip.txn_ref})."] if dup else (
        [] if slip.is_payment_slip else ["This doesn't look like a payment slip."])
    with conn.transaction():
        rid = conn.execute("""
            INSERT INTO receipts (file_path, file_hash, media_type, source, uploaded_by, status, amount, paid_at,
                                  sender_name, sender_bank, recipient_name, recipient_acct_last4, transfer_mode,
                                  reference, txn_ref, duitnow_ref, ai_confidence, ai_raw, flags, duplicate_of,
                                  tenancy_id, rent_schedule_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (path, digest, media, source, uploaded_by, status, slip.amount, parse_paid_at(slip.paid_at),
             slip.sender_name, slip.sender_bank, slip.recipient_name, slip.recipient_acct_last4,
             slip.transfer_mode, slip.reference, slip.txn_ref, slip.duitnow_ref,
             round(slip.confidence, 2), Jsonb(slip.model_dump()), flags, dup["id"] if dup else None,
             tenancy_id, schedule_id)
        ).fetchone()["id"]
        _audit(conn, uploaded_by, "upload_receipt", "receipts", rid, None,
               {"file": filename, "status": status, "amount": slip.amount})
    if status != "review":
        return {"receipt_id": rid, "status": status, "flags": flags,
                "message": "Duplicate of an earlier slip." if dup else "Not a payment slip."}
    d = match_receipt(conn, rid, uploaded_by)
    return {"receipt_id": rid, "status": d.status, "flags": d.flags,
            "message": "Checked against the bank and recorded." if d.status == "matched" else "Needs review."}


def match_receipt(conn, rid: int, actor_id: int) -> Decision:
    r = conn.execute("SELECT * FROM receipts WHERE id = %s", (rid,)).fetchone()
    if not r or r["status"] != "review":
        return Decision(r["status"] if r else "missing")
    credits, names, line, up_to = _context(conn, r)
    d = decide(_slip_dict(r), credits, names, line, company_last4(), today_myt(), up_to)
    if r["no_auto"]:
        d.flags = [UNDONE] + [f for f in d.flags if f != UNDONE]
        if d.status == "matched":
            d.status = "review"
    if d.status == "matched" and _auto_record(conn, r, d, actor_id):
        return d
    if d.status == "matched":                    # lost a race for the bank credit
        d.status, d.flags = "review", d.flags + ["That bank credit was just taken by another record: check."]
    conn.execute("UPDATE receipts SET flags = %s, bank_txn_id = %s WHERE id = %s AND status = 'review'",
                 (d.flags, d.bank_txn_id, rid))
    return d


def _auto_record(conn, r, d: Decision, actor_id) -> bool:
    try:
        with conn.transaction():
            pid, created = _attach_payment(conn, r, r["tenancy_id"], r["rent_schedule_id"], d.bank_txn_id,
                                           float(r["amount"]), r["paid_at"].date(), actor_id, d.link_payment_id)
            n = conn.execute("""UPDATE receipts SET status = 'matched', flags = '{}', bank_txn_id = %s,
                                    payment_id = %s, created_payment = %s
                                WHERE id = %s AND status = 'review'""",
                             (d.bank_txn_id, pid, created, r["id"])).rowcount
            if not n:
                raise psycopg.Rollback()
            _audit(conn, actor_id, "auto_match_receipt", "receipts", r["id"], None,
                   {"payment_id": pid, "linked_existing": not created, "bank_txn_id": d.bank_txn_id,
                    "tenancy_id": r["tenancy_id"], "amount": float(r["amount"])})
            return True
    except _Taken:
        pass
    return False


class _Taken(Exception):
    """The bank credit was matched by someone else between reading and writing."""


def _attach_payment(conn, r, tenancy_id, schedule_id, bank_txn_id, amount, paid_date, actor_id, link_payment_id=None):
    """Link the slip to an existing payment, or create one (and mark the bank credit). Returns (id, created)."""
    if link_payment_id:
        conn.execute("UPDATE payments SET receipt_path = COALESCE(receipt_path, %s) WHERE id = %s",
                     (r["file_path"], link_payment_id))
        return link_payment_id, False
    pid = conn.execute("""INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date, method, reference,
                                                receipt_path, bank_txn_id, recorded_by)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                       (tenancy_id, schedule_id, amount, paid_date, _method(r["transfer_mode"]),
                        (r["txn_ref"] or r["reference"] or "slip")[:100], r["file_path"], bank_txn_id,
                        actor_id)).fetchone()["id"]
    if bank_txn_id and not conn.execute("""UPDATE bank_transactions SET status = 'matched', matched_payment_id = %s
                                            WHERE id = %s AND status = 'unmatched'""",
                                         (pid, bank_txn_id)).rowcount:
        raise _Taken()
    return pid, True


def rematch_pending(conn, actor_id) -> dict:
    """Re-run matching for every slip waiting for review (after new bank credits arrive)."""
    counts = {"matched": 0, "review": 0}
    for row in conn.execute("SELECT id FROM receipts WHERE status = 'review' ORDER BY id").fetchall():
        d = match_receipt(conn, row["id"], actor_id)
        counts[d.status] = counts.get(d.status, 0) + 1
    return counts


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_upload(conn, digest):
    """An earlier upload of this exact file (screenshot or CSV), with who/when and what was read."""
    return conn.execute("""SELECT up.*, s.name AS uploaded_by_name FROM bank_uploads up
                           LEFT JOIN staff s ON s.id = up.uploaded_by WHERE up.file_hash = %s""", (digest,)).fetchone()


def start_upload(conn, digest, file_name, source, label, actor_id, *, taken_on=None, credits=None, notes=None) -> int:
    """Remember a file as soon as it is read, so it is never read (or paid for) again."""
    return conn.execute("""
        INSERT INTO bank_uploads (file_hash, file_name, source, account_label, taken_on, credits, notes, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_hash) DO UPDATE SET file_name = bank_uploads.file_name
        RETURNING id""", (digest, file_name, source, label, taken_on, Jsonb(credits) if credits is not None else None,
                          notes, actor_id)).fetchone()["id"]


def looks_saved(conn, label: str, credits: list[dict]) -> list:
    """For each credit, the date of an already-saved line with the same account, text and amount within a day
    (a screenshot read again with a different 'taken on' date), else None."""
    out = []
    for c in credits:
        row = conn.execute("""SELECT txn_date FROM bank_transactions
                              WHERE account_label = %s AND amount = %s AND description = %s
                                AND txn_date BETWEEN %s::date - 1 AND %s::date + 1 LIMIT 1""",
                           (label, float(c["amount"]), (c.get("description") or "").strip(),
                            c["txn_date"], c["txn_date"])).fetchone()
        out.append(row["txn_date"] if row else None)
    return out


def import_bank_credits(conn, credits: list[dict], label: str, actor_id: int, source: str, batch: str,
                        upload_id=None) -> int:
    """Insert credits ({txn_date, description, amount, [reference]}); skips ones already there.
    Names are never picked out of the text: matching reads the description exactly as the bank wrote it."""
    added = 0
    with conn.transaction():
        for c in credits:
            r = SimpleNamespace(txn_date=c["txn_date"], amount=float(c["amount"]),
                                description=(c.get("description") or "").strip(), reference=c.get("reference") or "")
            added += conn.execute("""
                INSERT INTO bank_transactions (account_label, txn_date, description, reference, amount,
                                               import_batch, row_hash, payer_name, source, upload_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (row_hash) DO NOTHING""",
                (label, r.txn_date, r.description, r.reference or None, r.amount, batch,
                 row_hash(label, r), None, source, upload_id)).rowcount
        if upload_id:
            conn.execute("""UPDATE bank_uploads SET added_at = NOW(), added = %s, total = %s, account_label = %s
                            WHERE id = %s""", (added, len(credits), label, upload_id))
        _audit(conn, actor_id, "bank_upload", "bank_transactions", None, None,
               {"label": label, "source": source, "rows": len(credits), "added": added, "batch": batch,
                "upload_id": upload_id})
    return added


# ── staff decisions ──────────────────────────────────────────────────────────
def confirm_receipt(conn, rid, actor_id, *, bank_txn_id=None, amount=None, paid_date=None,
                    remember_payer=None, note=None) -> str:
    """Staff confirm a slip in review (tenant and month are the ones it was uploaded for).
    Returns a message; raises ValueError for anything not allowed."""
    with conn.transaction():
        r = conn.execute("SELECT * FROM receipts WHERE id = %s AND status = 'review' FOR UPDATE", (rid,)).fetchone()
        if not r:
            raise ValueError("Someone else already handled this slip.")
        tenancy_id, schedule_id = r["tenancy_id"], r["rent_schedule_id"]
        amount = float(amount if amount is not None else r["amount"])
        paid_date = paid_date or (r["paid_at"].date() if r["paid_at"] else today_myt())
        link = None
        if bank_txn_id:
            bt = conn.execute("SELECT status, matched_payment_id FROM bank_transactions WHERE id = %s FOR UPDATE",
                              (bank_txn_id,)).fetchone()
            if bt["status"] == "matched" and bt["matched_payment_id"]:
                link = bt["matched_payment_id"]
            elif bt["status"] != "unmatched":
                raise ValueError("That bank credit is marked ignored.")
        if not link and not schedule_id:
            raise ValueError("This slip isn't linked to a rent month.")
        try:
            pid, created = _attach_payment(conn, r, tenancy_id, schedule_id, bank_txn_id, amount, paid_date,
                                           actor_id, link)
        except _Taken:
            raise ValueError("That bank credit was just matched to something else.")
        if remember_payer:
            alias = norm_alias(remember_payer)
            tenant_id = conn.execute("SELECT tenant_id FROM tenancies WHERE id = %s", (tenancy_id,)).fetchone()["tenant_id"]
            if alias:
                conn.execute("""INSERT INTO payer_aliases (tenant_id, alias_norm, created_by) VALUES (%s, %s, %s)
                                ON CONFLICT DO NOTHING""", (tenant_id, alias, actor_id))
        conn.execute("""UPDATE receipts SET status = 'confirmed', amount = %s, bank_txn_id = %s, payment_id = %s,
                            created_payment = %s, reviewed_by = %s, reviewed_at = NOW(), note = %s
                        WHERE id = %s""",
                     (amount, bank_txn_id, pid, created, actor_id, note, rid))
        _audit(conn, actor_id, "confirm_receipt", "receipts", rid, {"status": "review", "flags": r["flags"]},
               {"payment_id": pid, "linked_existing": not created, "bank_txn_id": bank_txn_id,
                "tenancy_id": tenancy_id, "amount": amount, "alias_saved": norm_alias(remember_payer or "") or None})
    return "Linked to the payment already recorded." if not created else "Payment recorded."


def reject_receipt(conn, rid, actor_id, reason: str) -> bool:
    with conn.transaction():
        n = conn.execute("""UPDATE receipts SET status = 'rejected', reviewed_by = %s, reviewed_at = NOW(), note = %s
                            WHERE id = %s AND status = 'review'""", (actor_id, reason, rid)).rowcount
        if n:
            _audit(conn, actor_id, "reject_receipt", "receipts", rid, {"status": "review"},
                   {"status": "rejected", "reason": reason})
    return bool(n)


def undo_receipt(conn, rid, actor_id) -> bool:
    """Back to review. A payment the match created is voided (kept, with a reason); a linked one is untouched."""
    with conn.transaction():
        r = conn.execute("""SELECT * FROM receipts WHERE id = %s AND status IN ('matched', 'confirmed')
                            FOR UPDATE""", (rid,)).fetchone()
        if not r:
            return False
        if r["created_payment"] and r["payment_id"]:
            conn.execute("""UPDATE payments SET voided = TRUE, voided_by = %s, voided_at = NOW(),
                                void_reason = 'Receipt match undone', bank_txn_id = NULL
                            WHERE id = %s AND NOT voided""", (actor_id, r["payment_id"]))
            conn.execute("""UPDATE bank_transactions SET status = 'unmatched', matched_payment_id = NULL
                            WHERE matched_payment_id = %s""", (r["payment_id"],))
        conn.execute("""UPDATE receipts SET status = 'review', payment_id = NULL, created_payment = FALSE,
                            no_auto = TRUE, flags = %s, reviewed_by = NULL, reviewed_at = NULL
                        WHERE id = %s""", ([UNDONE], rid))
        _audit(conn, actor_id, "undo_receipt", "receipts", rid,
               {"status": r["status"], "payment_id": r["payment_id"]},
               {"status": "review", "payment_voided": bool(r["created_payment"])})
    return True


# ── money in the bank with no slip ───────────────────────────────────────────
BANK_UNDONE = "Bank auto-match undone"


def _people(conn) -> list[dict]:
    """Tenancies with rent still owed: their names (tenant + saved payer names) and unpaid months."""
    people = {}
    for r in conn.execute("""SELECT tenancy_id, tenant_name, schedule_id, period, due_date, balance
                             FROM v_rent_status WHERE balance > 0 ORDER BY due_date""").fetchall():
        p = people.setdefault(r["tenancy_id"], {"tenancy_id": r["tenancy_id"], "names": [r["tenant_name"]],
                                                "lines": []})
        p["lines"].append(dict(r))
    if people:
        for a in conn.execute("""SELECT t.id AS tenancy_id, a.alias_norm FROM tenancies t
                                 JOIN payer_aliases a ON a.tenant_id = t.tenant_id
                                 WHERE t.id = ANY(%s)""", (list(people),)).fetchall():
            people[a["tenancy_id"]]["names"].append(a["alias_norm"])
    return list(people.values())


def _open_credits(conn) -> list[dict]:
    """Recent unmatched bank credits that no slip is waiting on."""
    return conn.execute("""
        SELECT b.* FROM bank_transactions b
        WHERE b.status = 'unmatched' AND b.amount > 0 AND b.txn_date >= CURRENT_DATE - 120
          AND NOT EXISTS (SELECT 1 FROM receipts x WHERE x.bank_txn_id = b.id
                          AND x.status IN ('review', 'matched', 'confirmed'))
        ORDER BY b.txn_date, b.id""").fetchall()


def bank_hints(conn) -> dict:
    """{tenancy_id: [(credit, BankPick)]} for credits under a tenant's name not recorded yet (Rent Board)."""
    people = _people(conn)
    hints = {}
    for c in _open_credits(conn):
        pick = bank_pick(c, people)
        if pick.tenancy_id:
            hints.setdefault(pick.tenancy_id, []).append((c, pick))
    return hints


def _record_credit(conn, credit, tenancy_id, parts, actor_id, auto) -> list[int]:
    """parts: [(schedule_id, amount)] adding up to the credit. One payment per month, all pointing at the credit."""
    with conn.transaction():
        pids = [conn.execute("""INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date, method, reference,
                                                      bank_txn_id, recorded_by, auto_from_bank)
                                VALUES (%s, %s, %s, %s, 'bank_transfer', %s, %s, %s, %s) RETURNING id""",
                             (tenancy_id, sid, amt, credit["txn_date"], (credit["description"] or "bank")[:100],
                              credit["id"], actor_id, auto)).fetchone()["id"] for sid, amt in parts]
        if not conn.execute("""UPDATE bank_transactions SET status = 'matched', matched_payment_id = %s
                               WHERE id = %s AND status = 'unmatched'""", (pids[0], credit["id"])).rowcount:
            raise psycopg.Rollback()                          # someone else took it meanwhile
        _audit(conn, actor_id, "bank_auto_record" if auto else "bank_record", "payments", pids[0], None,
               {"bank_txn_id": credit["id"], "tenancy_id": tenancy_id, "payments": pids,
                "parts": [[sid, amt] for sid, amt in parts]})
        return pids
    return []


def auto_from_bank(conn, actor_id) -> int:
    """Record every credit that clearly belongs to one tenant (name + amount no more than owed). Returns count."""
    n = 0
    for c in _open_credits(conn):
        if c["no_auto"]:
            continue
        pick = bank_pick(c, _people(conn))                    # re-read: the previous credit changed balances
        if pick.auto and _record_credit(conn, c, pick.tenancy_id, [(pick.line["schedule_id"], float(c["amount"]))],
                                        actor_id, True):
            n += 1
    return n


def split_for(conn, credit, tenancy_id) -> list[tuple[dict, float]]:
    """How a credit would be split over this tenancy's unpaid months (oldest first)."""
    lines = [dict(r) for r in conn.execute("""SELECT schedule_id, period, due_date, balance FROM v_rent_status
                                              WHERE tenancy_id = %s ORDER BY due_date""", (tenancy_id,)).fetchall()]
    return allocate(float(credit["amount"]), lines)


def record_bank_credit(conn, txn_id, tenancy_id, actor_id) -> bool:
    """Staff: this bank credit is this tenant's rent, split over unpaid months oldest first."""
    c = conn.execute("SELECT * FROM bank_transactions WHERE id = %s AND status = 'unmatched'", (txn_id,)).fetchone()
    if not c:
        return False
    parts = [(ln["schedule_id"], amt) for ln, amt in split_for(conn, c, tenancy_id)]
    return bool(parts and _record_credit(conn, c, tenancy_id, parts, actor_id, False))


def undo_bank_payment(conn, payment_id, actor_id) -> bool:
    """Void the payment(s) made from one bank credit; the credit goes back to unmatched and is never
    auto-recorded again."""
    with conn.transaction():
        p = conn.execute("SELECT bank_txn_id FROM payments WHERE id = %s AND NOT voided AND bank_txn_id IS NOT NULL",
                         (payment_id,)).fetchone()
        if not p:
            return False
        voided = [r["id"] for r in conn.execute(
            """UPDATE payments SET voided = TRUE, voided_by = %s, voided_at = NOW(), void_reason = %s, bank_txn_id = NULL
               WHERE bank_txn_id = %s AND NOT voided RETURNING id""",
            (actor_id, BANK_UNDONE, p["bank_txn_id"])).fetchall()]
        conn.execute("""UPDATE bank_transactions SET status = 'unmatched', matched_payment_id = NULL, no_auto = TRUE
                        WHERE id = %s""", (p["bank_txn_id"],))
        _audit(conn, actor_id, "undo_bank_payment", "payments", payment_id, None,
               {"bank_txn_id": p["bank_txn_id"], "voided": voided})
    return True
