"""Bank statement matching: upload CSV → suggested matches → staff confirm.

Automation suggests, staff confirm: nothing is matched until someone clicks ✓ Confirm.
"""
import datetime as dt

import psycopg
import streamlit as st

import receipts_core as rc
from bank import BANK_FORMATS, CUSTOM, load_statement, read_csv, strong
from db import audit, pool, query

user = st.session_state.user
st.title("Bank Matching")

def recheck_slips():
    """New bank credits may complete slips waiting for review, or pay rent under a tenant's name (no slip)."""
    with pool().connection() as conn:
        c = rc.rematch_pending(conn, user["id"])
        n = rc.auto_from_bank(conn, user["id"])
    if c.get("matched"):
        st.success(f"🧾 {c['matched']} waiting slip(s) now matched and recorded automatically.")
    if n:
        st.success(f"🏦 {n} payment(s) under a tenant's name recorded automatically (no slip needed).")


def show(df):
    if df.empty:
        st.success("None.")
    else:
        st.dataframe(df, hide_index=True, width="stretch")


upload_tab, match_tab, done_tab, gaps_tab = st.tabs(["Upload", "Match", "Matched / ignored", "Gaps"])

# ── Upload ───────────────────────────────────────────────────────────────────
with upload_tab:
    st.caption("Screenshots of the bank app: use the 🏦 bank statement bar at the top of the Rent Board (it never "
               "reads the same file twice). Here: CSV statements, including new bank formats.")
    choice = st.selectbox("Bank format", list(BANK_FORMATS) + [CUSTOM])
    file = st.file_uploader("Bank statement (CSV)", type=["csv"])
    fmt, label = None, choice
    if choice == CUSTOM and file:
        skip = st.number_input("Lines above the header row", min_value=0, max_value=50, value=0)
        try:
            cols = list(read_csv(file, skip).columns)
        except Exception as e:
            st.error(f"Could not read the file: {e}")
            cols = []
    if choice == CUSTOM and file and cols:
        st.caption(f"Columns found: {cols}")
        c1, c2 = st.columns(2)
        label = c1.text_input("Account label (keep the same for every upload of this account)",
                              value="Company Bank")
        date_col = c2.selectbox("Date column", cols)
        desc_col = c1.selectbox("Description column", cols, index=min(1, len(cols) - 1))
        ref_col = c2.selectbox("Reference column", ["(none)"] + cols)
        kind = c1.radio("Money in is…", ["a Credit column", "a signed Amount column",
                                         "an Amount column + DR/CR column"])
        fmt = {"skiprows": int(skip), "date": date_col, "desc": desc_col,
               "ref": None if ref_col == "(none)" else ref_col,
               "date_format": c2.text_input("Date format (blank = guess, day first)", "%d/%m/%Y") or None}
        if kind == "a Credit column":
            fmt["credit"] = c2.selectbox("Credit column", cols)
        else:
            fmt["amount"] = c2.selectbox("Amount column", cols)
            if kind.startswith("an Amount"):
                fmt["drcr"] = c2.selectbox("DR/CR column", cols)
                fmt["credit_marker"] = c2.text_input("Value meaning money in", "CR")
        with st.expander("Save this mapping permanently"):
            st.caption("Paste into BANK_FORMATS in portal/bank.py:")
            st.code(f'"{label}": {fmt!r},', language="python")
    elif choice != CUSTOM:
        fmt = BANK_FORMATS[choice]

    if file and fmt and label.strip():
        try:
            stmt = load_statement(file, fmt)
        except Exception as e:
            st.error(str(e))
            stmt = None
        if stmt is None:
            pass
        elif stmt.empty:
            st.warning("No incoming payments found. Check the mapping.")
        else:
            st.write(f"**{len(stmt)} incoming payment(s)**, {stmt['txn_date'].min():%d/%m/%Y} to "
                     f"{stmt['txn_date'].max():%d/%m/%Y}, total RM{stmt['amount'].sum():,.2f}")
            st.dataframe(stmt, hide_index=True, width="stretch", height=250)
            digest = rc.file_hash(file.getvalue())
            with pool().connection() as conn:
                prev = rc.find_upload(conn, digest)
            if prev and prev["added_at"]:
                st.info(f"This exact file was already uploaded on {prev['uploaded_at']:%d %b %Y %H:%M}: "
                        f"{prev['added'] or 0} new of {prev['total'] or 0} saved then.")
            elif st.button(f"Save {len(stmt)} incoming payment(s)", type="primary"):
                rows = stmt.to_dict("records")
                with pool().connection() as conn:
                    uid = rc.start_upload(conn, digest, file.name, "csv", label.strip(), user["id"])
                    added = rc.import_bank_credits(conn, rows, label.strip(), user["id"], "csv",
                                                   f"{file.name} @ {dt.datetime.now():%Y-%m-%d %H:%M}", uid)
                st.success(f"Saved {added} new incoming payment(s); {len(rows) - added} were already saved.")
                recheck_slips()

# ── Match ────────────────────────────────────────────────────────────────────
with match_tab:
    txns = query("""SELECT id, account_label, txn_date, description, reference, amount
                    FROM bank_transactions WHERE status = 'unmatched'
                    ORDER BY txn_date, id""")
    if txns.empty:
        st.success("No unmatched bank credits. Upload a statement in the Upload tab.")
    else:
        min_date = txns["txn_date"].min()
        lines = query("""SELECT schedule_id, tenancy_id, unit_code, tenant_name, period,
                                balance, due_date
                         FROM v_rent_status WHERE status <> 'PAID'
                         ORDER BY unit_code, period""")
        pays = query("""SELECT p.id AS payment_id, p.amount, p.paid_date, p.method, p.reference,
                               u.code AS unit_code, tn.name AS tenant_name, rs.period
                        FROM payments p
                        JOIN tenancies t ON t.id = p.tenancy_id
                        JOIN units u ON u.id = t.unit_id
                        JOIN tenants tn ON tn.id = t.tenant_id
                        LEFT JOIN rent_schedule rs ON rs.id = p.rent_schedule_id
                        WHERE NOT p.voided AND p.bank_txn_id IS NULL AND p.method <> 'cash'
                          AND p.paid_date >= %s::date - 7""", (min_date,))

        LIMIT = 30
        st.caption(f"{len(txns)} unmatched credit(s)" + (f", showing the first {LIMIT}" if len(txns) > LIMIT else "")
                   + ". ✓ Confirm creates or links the payment; Ignore is for non-rent credits.")
        line_label = lambda r: (f"{r['unit_code']} · {r['tenant_name']} · {r['period']:%b %Y} · "
                                f"balance RM{r['balance']:,.2f}")

        for tx in txns.head(LIMIT).itertuples():
            amount = float(tx.amount)
            text = f"{tx.description} {tx.reference or ''}"
            options = []                                     # (strong, kind, id, label)
            if not pays.empty:
                near = pays[(pays["amount"].astype(float).sub(amount).abs() < 0.01)
                            & (pays["paid_date"].map(lambda d: abs((d - tx.txn_date).days) <= 7))]
                for p in near.to_dict("records"):
                    month = f"{p['period']:%b %Y}" if p["period"] else "-"
                    options.append((strong(text, p["unit_code"], p["tenant_name"]), "payment", p["payment_id"],
                                    f"Already recorded: {p['unit_code']} · {p['tenant_name']} · {month} · "
                                    f"paid {p['paid_date']:%d/%m} ({p['method']})"))
            if not lines.empty:
                near = lines[(lines["balance"].astype(float).sub(amount).abs() < 0.01)
                             & (lines["due_date"].map(lambda d: abs((d - tx.txn_date).days) <= 20))]
                for r in near.to_dict("records"):
                    options.append((strong(text, r["unit_code"], r["tenant_name"]), "line",
                                    r["schedule_id"], "New payment: " + line_label(r)))
            options.sort(key=lambda o: (not o[0], o[1] != "payment"))
            n_strong = sum(o[0] for o in options)

            with st.container(border=True):
                st.markdown(f"**{tx.txn_date:%d/%m/%Y} · RM{amount:,.2f}** · {tx.description}"
                            f"{' · ref ' + tx.reference if tx.reference else ''} · _{tx.account_label}_")
                choices = [(o[1], o[2]) for o in options] + [("other", None)]
                names = {(o[1], o[2]): ("⭐ Suggested · " if o[0] and n_strong == 1 else "") + o[3]
                         for o in options}
                names[("other", None)] = "Another month's rent…" if options else "No exact match: choose the rent it pays…"
                pick = st.selectbox("Match to", choices, key=f"pick{tx.id}", format_func=names.get,
                                    label_visibility="collapsed")
                kind, target = pick
                if kind == "other":
                    if lines.empty:
                        st.caption("No unpaid rent.")
                        target = None
                    else:
                        recs = lines.to_dict("records")
                        target = st.selectbox("Rent it pays", [r["schedule_id"] for r in recs], key=f"other{tx.id}",
                                              index=None, placeholder="Choose unit and month (or Ignore)",
                                              format_func=dict((r["schedule_id"], line_label(r)) for r in recs).get)
                        kind = "line"
                c1, c2, c3 = st.columns([1, 1, 3])
                note = c3.text_input("Note (for Ignore)", key=f"note{tx.id}",
                                     placeholder="e.g. landlord top-up, deposit",
                                     label_visibility="collapsed")
                if c1.button("✓ Confirm", key=f"ok{tx.id}", type="primary", disabled=target is None):
                    ok = False
                    with pool().connection() as conn:
                        try:
                            with conn.transaction():
                                if kind == "payment":
                                    pid = int(target)
                                    if not conn.execute("""UPDATE payments SET bank_txn_id = %s
                                                           WHERE id = %s AND bank_txn_id IS NULL
                                                             AND NOT voided""",
                                                        (int(tx.id), pid)).rowcount:
                                        raise psycopg.Rollback()
                                else:
                                    sched = conn.execute("SELECT tenancy_id FROM rent_schedule WHERE id = %s",
                                                         (int(target),)).fetchone()
                                    pid = conn.execute("""
                                        INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date,
                                            method, reference, bank_txn_id, recorded_by)
                                        VALUES (%s, %s, %s, %s, 'bank_transfer', %s, %s, %s) RETURNING id""",
                                        (sched["tenancy_id"], int(target), amount, tx.txn_date,
                                         (tx.reference or tx.description or "")[:100] or None,
                                         int(tx.id), user["id"])).fetchone()["id"]
                                if not conn.execute("""UPDATE bank_transactions
                                                       SET status = 'matched', matched_payment_id = %s
                                                       WHERE id = %s AND status = 'unmatched'""",
                                                    (pid, int(tx.id))).rowcount:
                                    raise psycopg.Rollback()
                                audit(conn, user["id"], "bank_match", "bank_transactions", int(tx.id),
                                      {"status": "unmatched"},
                                      {"status": "matched", "payment_id": pid,
                                       "linked_existing": kind == "payment", "amount": amount})
                                ok = True
                        except psycopg.errors.Error as e:
                            st.error(f"Could not save: {str(e).splitlines()[0]}")
                    if ok:
                        st.toast("Matched."); st.rerun()
                    else:
                        st.warning("Someone else already handled this transaction or payment. Reloading.")
                        st.rerun()
                if c2.button("Ignore", key=f"ig{tx.id}"):
                    with pool().connection() as conn, conn.transaction():
                        if conn.execute("""UPDATE bank_transactions SET status = 'ignored'
                                           WHERE id = %s AND status = 'unmatched'""",
                                        (int(tx.id),)).rowcount:
                            audit(conn, user["id"], "bank_ignore", "bank_transactions", int(tx.id),
                                  {"status": "unmatched"}, {"status": "ignored", "note": note.strip() or None})
                    st.rerun()

# ── Matched / ignored (with undo) ────────────────────────────────────────────
with done_tab:
    done = query("""SELECT b.id, b.txn_date, b.amount, b.description, b.status, b.matched_payment_id,
                           u.code AS unit_code, rs.period, p.voided,
                           COALESCE(a.after->>'linked_existing' = 'false', FALSE) AS created_by_match
                    FROM bank_transactions b
                    LEFT JOIN payments p ON p.id = b.matched_payment_id
                    LEFT JOIN tenancies t ON t.id = p.tenancy_id
                    LEFT JOIN units u ON u.id = t.unit_id
                    LEFT JOIN rent_schedule rs ON rs.id = p.rent_schedule_id
                    LEFT JOIN LATERAL (SELECT after FROM audit_log
                                       WHERE entity = 'bank_transactions' AND entity_id = b.id
                                         AND action = 'bank_match'
                                       ORDER BY created_at DESC LIMIT 1) a ON TRUE
                    WHERE b.status <> 'unmatched'
                    ORDER BY b.txn_date DESC, b.id DESC LIMIT 100""")
    if done.empty:
        st.info("Nothing matched or ignored yet.")
    for r in done.itertuples():
        what = (f"→ {r.unit_code} {r.period:%b %Y}" if r.status == "matched" and r.period is not None
                else r.status.upper())
        c1, c2 = st.columns([6, 1])
        c1.write(f"{r.txn_date:%d/%m/%Y} · RM{float(r.amount):,.2f} · {r.description} · **{what}**")
        if c2.button("Undo", key=f"undo{r.id}"):
            with pool().connection() as conn, conn.transaction():
                n = conn.execute("""UPDATE bank_transactions SET status = 'unmatched', matched_payment_id = NULL
                                    WHERE id = %s AND status = %s""", (int(r.id), r.status)).rowcount
                if n and r.status == "matched":
                    if r.created_by_match:          # payment came from this match: void it, keep history
                        conn.execute("""UPDATE payments SET voided = TRUE, voided_by = %s, voided_at = NOW(),
                                            void_reason = 'Bank match undone', bank_txn_id = NULL
                                        WHERE id = %s AND NOT voided""", (user["id"], int(r.matched_payment_id)))
                    else:                           # payment was recorded by staff: just unlink it
                        conn.execute("UPDATE payments SET bank_txn_id = NULL WHERE id = %s",
                                     (int(r.matched_payment_id),))
                if n:
                    audit(conn, user["id"], "bank_undo", "bank_transactions", int(r.id),
                          {"status": r.status, "payment_id": r.matched_payment_id},
                          {"status": "unmatched", "payment_voided": bool(r.created_by_match)})
            st.rerun()

# ── Gaps ─────────────────────────────────────────────────────────────────────
with gaps_tab:
    periods = query("SELECT DISTINCT period FROM rent_schedule ORDER BY period DESC")
    if periods.empty:
        st.info("No monthly rent yet."); st.stop()
    period = st.selectbox("Month", periods["period"].tolist(), format_func=lambda d: f"{d:%B %Y}",
                          key="gap_month")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("In the bank, not matched")
        df = query("""SELECT txn_date, amount, description, reference FROM bank_transactions
                      WHERE status = 'unmatched'
                        AND date_trunc('month', txn_date) = %s ORDER BY txn_date""", (period,))
        show(df)
    with c2:
        st.subheader("Rent not yet paid")
        df = query("""SELECT unit_code, tenant_name, balance, due_date, status FROM v_rent_status
                      WHERE period = %s AND status <> 'PAID' ORDER BY unit_code""", (period,))
        show(df)
    st.subheader("Recorded in the portal, not found in the bank")
    st.caption("Non-cash payments dated this month with no matched bank credit. Check they really arrived.")
    df = query("""SELECT u.code AS unit_code, tn.name AS tenant_name, p.amount, p.paid_date, p.method,
                         p.reference, COALESCE(s.name, 'import') AS recorded_by
                  FROM payments p
                  JOIN tenancies t ON t.id = p.tenancy_id
                  JOIN units u ON u.id = t.unit_id
                  JOIN tenants tn ON tn.id = t.tenant_id
                  LEFT JOIN staff s ON s.id = p.recorded_by
                  WHERE NOT p.voided AND p.bank_txn_id IS NULL AND p.method <> 'cash'
                    AND date_trunc('month', p.paid_date) = %s
                  ORDER BY p.paid_date""", (period,))
    show(df)
