"""Screen pieces for transfer slips, shared by the Rent Board and the Slips to review page.

Workflow: pick a tenant's rent month → upload their slip → AI reads it → the bank statement must show
the same amount under the tenant's exact name → recorded (PAID / partly paid), else Needs review here.
"""
import datetime as dt
import os

import pandas as pd
import streamlit as st

import receipts_core as rc
from bank import BANK_FORMATS, load_statement
from db import pool, query
from messages import breakdown
from receipt_ai import AIError, media_type_for, read_bank_history
from receipt_match import name_in_text, payer_text
from storage import receipt_bytes

STATUS = {"matched": "✅ Checked with bank · recorded", "confirmed": "☑️ Confirmed by staff",
          "review": "🟡 Needs review", "duplicate": "⚪ Duplicate", "not_slip": "✖ Not a slip",
          "rejected": "🚫 Rejected"}


def _can_edit(user):
    return user["role"] != "viewer"


def _after_import(user, added, total):
    """Re-check waiting slips, remember the message, and redraw (so the bar shows the new date)."""
    with pool().connection() as conn:
        c = rc.rematch_pending(conn, user["id"])
        n = rc.auto_from_bank(conn, user["id"])
    msg = f"Saved {added} new incoming payment(s); {total - added} were already saved."
    if c.get("matched"):
        msg += f" {c['matched']} slip(s) in review now checked with the bank and recorded."
    if n:
        msg += f" {n} payment(s) under a tenant's name recorded (no slip needed)."
    st.session_state.bb_flash = msg
    st.rerun()


def show_file(path, width=240):
    try:
        data = receipt_bytes(path)
    except Exception as e:                                   # file missing / storage not set up
        st.caption(f"(slip file not available: {e})")
        return
    if path.lower().endswith(".pdf"):
        st.download_button("Download slip (PDF)", data, file_name=path.rsplit("/", 1)[-1], key=f"dl{path}")
    else:
        st.image(data, width=width)


# ── Bank statement bar ───────────────────────────────────────────────────────
def bank_bar(user):
    info = query("SELECT max(txn_date) AS d, count(*) AS n FROM bank_transactions").iloc[0]
    upto = f"up to **{info['d']:%d %b %Y}**" if info["d"] is not None else "**not loaded yet**"
    flash = st.session_state.pop("bb_flash", None)
    if flash:
        st.success(f"🏦 {flash}")
    with st.expander(f"🏦 Bank statement {upto} · update it here", expanded=info["d"] is None):
        st.caption("Only money coming in is kept. After saving: money under a tenant's exact name is recorded as "
                   "rent (no slip needed; a part payment leaves the remainder owing), and slips waiting in review "
                   "are checked again. The same file is never read twice.")
        shot_tab, csv_tab, log_tab = st.tabs(["📱 Screenshot of the bank app (AI)", "📄 CSV statement",
                                              "📜 Statement log"])
        with log_tab:
            bank_log()
        if not _can_edit(user):
            for t in (shot_tab, csv_tab):
                t.caption("View only.")
            return
        with shot_tab:
            _screenshot_upload(user)
        with csv_tab:
            _csv_upload(user)


def _already(prev):
    who = prev["uploaded_by_name"] or "someone"
    st.info(f"This exact file was already uploaded on {prev['uploaded_at']:%d %b %Y, %H:%M} by {who}: "
            f"{prev['added'] or 0} new of {prev['total'] or 0} incoming payment(s) were saved then. "
            "Nothing to do (it isn't read again).")


def _screenshot_upload(user):
    c1, c2 = st.columns(2)
    label = c1.text_input("Bank account", value="Company Bank", key="bb_label",
                          help="Keep the same name every time for the same account")
    taken = c2.date_input("Screenshot taken on", value=rc.today_myt(), format="DD/MM/YYYY", key="bb_date",
                          help="So 'Today' and 'Yesterday' in the screenshot get the right dates")
    shot = st.file_uploader("Screenshot of the transaction history", type=["png", "jpg", "jpeg", "webp", "pdf"],
                            key="bb_file")
    if not shot:
        return
    data = shot.getvalue()
    digest = rc.file_hash(data)
    with pool().connection() as conn:
        prev = rc.find_upload(conn, digest)
    if prev and prev["added_at"]:
        _already(prev); return
    got = st.session_state.get("bb")
    if not got or got["digest"] != digest:
        got = None
        if prev and prev["credits"] is not None:                 # read before but not saved: reuse, no AI
            got = {"digest": digest, "upload_id": prev["id"], "credits": prev["credits"], "notes": prev["notes"],
                   "ok": True}
            st.caption(f"Read on {prev['uploaded_at']:%d %b %H:%M}: using that reading (no AI cost).")
        elif st.button("Read screenshot", key="bb_read"):
            try:
                with st.spinner("Reading the screenshot…"):
                    hist = read_bank_history(data, media_type_for(shot.name), taken)
            except AIError as e:
                st.error(str(e)); return
            credits = [{**c.model_dump(), "date": str(c.date)} for c in hist.credits]
            with pool().connection() as conn:
                uid = rc.start_upload(conn, digest, shot.name, "screenshot", label.strip(), user["id"],
                                      taken_on=taken, credits=credits, notes=hist.notes)
            got = {"digest": digest, "upload_id": uid, "credits": credits, "notes": hist.notes,
                   "ok": hist.is_bank_history}
        if got:
            st.session_state.bb = got
    if not got:
        return
    if not got["ok"]:
        st.warning("This doesn't look like a bank transaction history.")
    if got["notes"]:
        st.caption(f"Note: {got['notes']}")
    if not got["credits"]:
        st.info("No incoming money found in this screenshot.")
        return
    rows = []
    for c in got["credits"]:
        try:
            rows.append({**c, "txn_date": dt.date.fromisoformat(str(c["date"])[:10])})
        except ValueError:
            rows.append({**c, "txn_date": None})
    with pool().connection() as conn:
        seen = rc.looks_saved(conn, label.strip(), [r for r in rows if r["txn_date"]])
    seen_iter = iter(seen)
    table = pd.DataFrame([{"add": True, "date": str(r["date"]), "description": r["description"],
                           "amount": r["amount"], "already": ""} for r in rows])
    for i, r in enumerate(rows):
        hit = next(seen_iter) if r["txn_date"] else None
        if hit:
            table.loc[i, ["add", "already"]] = [False, f"⚠️ looks saved already ({hit:%d/%m})"]
    if (table["already"] != "").any():
        st.warning("Some lines look like they were saved before (same text and amount within a day): they are "
                   "unticked. Tick them only if they really are separate payments.")
    st.caption("Incoming money found (text copied exactly as shown; correct it if needed):")
    edited = st.data_editor(table, hide_index=True, width="stretch", num_rows="dynamic", key=f"bb_edit_{digest[:8]}",
                            disabled=["already"], column_config={
                                "add": st.column_config.CheckboxColumn("Save", default=True),
                                "date": "Date (YYYY-MM-DD)", "description": "Text as shown",
                                "amount": st.column_config.NumberColumn("Amount (RM)", format="%.2f"),
                                "already": "Check"})
    chosen = edited[edited["add"].fillna(True) & edited["amount"].notna()]
    if st.button(f"Save {len(chosen)} incoming payment(s)", type="primary", key="bb_add", disabled=chosen.empty):
        try:
            save_rows = [{"txn_date": dt.date.fromisoformat(str(r["date"])[:10]), "description": r["description"],
                          "amount": r["amount"]} for r in chosen.to_dict("records")]
        except ValueError:
            st.error("Every date must look like 2026-10-08."); return
        with pool().connection() as conn:
            added = rc.import_bank_credits(conn, save_rows, label.strip() or "Company Bank", user["id"], "screenshot",
                                           f"{shot.name} @ {dt.datetime.now():%Y-%m-%d %H:%M}", got["upload_id"])
        del st.session_state["bb"]
        _after_import(user, added, len(save_rows))


def _csv_upload(user):
    fmt_name = st.selectbox("Bank format", list(BANK_FORMATS), key="bb_fmt",
                            help="New bank format? An admin can map it under Bank Matching → Upload.")
    f = st.file_uploader("Statement (CSV)", type=["csv"], key="bb_csv")
    if not f:
        return
    digest = rc.file_hash(f.getvalue())
    with pool().connection() as conn:
        prev = rc.find_upload(conn, digest)
    if prev and prev["added_at"]:
        _already(prev); return
    try:
        stmt = load_statement(f, BANK_FORMATS[fmt_name])
    except Exception as e:
        st.error(str(e)); return
    st.caption(f"{len(stmt)} incoming payment(s) in this file. Lines saved before are skipped automatically.")
    if st.button(f"Save {len(stmt)} incoming payment(s)", type="primary", key="bb_csv_add"):
        rows = stmt.to_dict("records")
        with pool().connection() as conn:
            uid = rc.start_upload(conn, digest, f.name, "csv", fmt_name, user["id"])
            added = rc.import_bank_credits(conn, rows, fmt_name, user["id"], "csv",
                                           f"{f.name} @ {dt.datetime.now():%Y-%m-%d %H:%M}", uid)
        _after_import(user, added, len(rows))


HOW_ICONS = {"Not recorded yet": "⚪ Not recorded yet", "Ignored (not rent)": "➖ Ignored (not rent)",
             "Auto: tenant's name on the bank line": "🏦 Auto: tenant's name on the bank line",
             "Checked with a slip": "🧾 Checked with a slip", "Recorded by staff": "✓ Recorded by staff"}


def bank_log():
    """Every incoming payment saved from the bank, and what became of it."""
    months = query("SELECT DISTINCT date_trunc('month', txn_date)::date AS m FROM bank_transactions ORDER BY m DESC")
    if months.empty:
        st.caption("Nothing saved yet."); return
    c1, c2 = st.columns(2)
    m = c1.selectbox("Month", months["m"].tolist(), format_func=lambda d: f"{d:%B %Y}", key="bl_month")
    which = c2.selectbox("Show", ["All", "Not recorded yet", "Recorded"], key="bl_which")
    log = query("""SELECT * FROM v_bank_log WHERE txn_date >= %s AND txn_date < %s::date + INTERVAL '1 month'
                   ORDER BY txn_date DESC, id DESC""", (m, m))
    if which == "Not recorded yet":
        log = log[log["how"] == "Not recorded yet"]
    elif which == "Recorded":
        log = log[~log["how"].isin(["Not recorded yet", "Ignored (not rent)"])]
    if log.empty:
        st.caption("None."); return
    amt = log["amount"].astype(float)
    open_ = log["how"] == "Not recorded yet"
    k1, k2, k3 = st.columns(3)
    k1.metric("Incoming", f"RM{amt.sum():,.2f}", help=f"{len(log)} payment(s)")
    k2.metric("Recorded as rent", f"RM{amt[~open_ & (log['how'] != 'Ignored (not rent)')].sum():,.2f}")
    k3.metric("Not recorded yet", f"RM{amt[open_].sum():,.2f}", help=f"{int(open_.sum())} payment(s)")
    show = log.assign(how=log["how"].map(lambda h: HOW_ICONS.get(h, h)),
                      paid_for=log["paid_for"].fillna(""), file_name=log["file_name"].fillna("(before the log)"),
                      uploaded_by=log["uploaded_by"].fillna(""))
    st.dataframe(show[["txn_date", "description", "amount", "how", "paid_for", "account_label", "file_name",
                       "uploaded_by"]], hide_index=True, width="stretch", column_config={
        "txn_date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"), "description": "Text on the bank line",
        "amount": st.column_config.NumberColumn("Amount", format="RM %.2f"), "how": "What happened",
        "paid_for": "Paid for", "account_label": "Bank account", "file_name": "From file",
        "uploaded_by": "Uploaded by"})
    ups = query("""SELECT up.uploaded_at AT TIME ZONE 'Asia/Kuching' AS uploaded_at, s.name AS by, up.file_name,
                          up.source, up.account_label, up.total, up.added,
                          CASE WHEN up.added_at IS NULL THEN 'read, not saved' ELSE 'saved' END AS state
                   FROM bank_uploads up LEFT JOIN staff s ON s.id = up.uploaded_by
                   ORDER BY up.uploaded_at DESC LIMIT 30""")
    if not ups.empty:
        with st.expander(f"Uploaded files ({len(ups)})"):
            st.dataframe(ups, hide_index=True, width="stretch", column_config={
                "uploaded_at": st.column_config.DatetimeColumn("When", format="DD/MM/YYYY HH:mm"), "by": "By",
                "file_name": "File", "source": "Type", "account_label": "Bank account",
                "total": "Lines in file", "added": "New saved", "state": "State"})


# ── Review card (one slip) ───────────────────────────────────────────────────
def review_card(r: dict, user):
    rid = int(r["id"])
    left, right = st.columns([1, 2])
    with left:
        show_file(r["file_path"])
    with right:
        for flag in r["flags"] or []:
            st.warning(flag, icon="⚠️")
        info = [("Amount", f"RM{float(r['amount'] or 0):,.2f}"),
                ("Paid", f"{r['paid_at']:%d/%m/%Y %H:%M}" if r["paid_at"] is not None else None),
                ("To", f"{r['recipient_name'] or '?'} …{r['recipient_acct_last4'] or '????'}"),
                ("Reference", r["reference"]), ("Bank ref", r["txn_ref"])]
        st.markdown(" · ".join(f"**{k}:** {v}" for k, v in info if v))
        if not _can_edit(user):
            return
        amount = st.number_input("Amount (RM)", min_value=0.0, step=10.0, key=f"amt{rid}", value=float(r["amount"] or 0))
        paid = r["paid_at"].date() if r["paid_at"] is not None else rc.today_myt()
        credits = query("""SELECT id, txn_date, amount, description, matched_payment_id FROM bank_transactions
                           WHERE abs(amount - %s) < 0.01 AND txn_date BETWEEN %s::date - 3 AND %s::date + 5
                             AND status <> 'ignored'
                             AND NOT EXISTS (SELECT 1 FROM receipts x WHERE x.bank_txn_id = bank_transactions.id
                                             AND x.status IN ('matched', 'confirmed') AND x.id <> %s)
                           ORDER BY abs(txn_date - %s::date)""", (amount, paid, paid, rid, paid))
        rows = {c["id"]: c for c in credits.to_dict("records")} if not credits.empty else {}
        opts = list(rows) + [None]
        label = lambda v: ("Not in the bank statement: I've checked the money arrived" if v is None else
                           f"{rows[v]['txn_date']:%d/%m} · RM{float(rows[v]['amount']):,.2f} · “{rows[v]['description']}”"
                           + (" · already recorded" if rows[v]["matched_payment_id"] else ""))
        credit_id = st.selectbox("Matching bank credit", opts, format_func=label, key=f"bc{rid}",
                                 index=opts.index(r["bank_txn_id"]) if r["bank_txn_id"] in opts else 0)
        remember = None
        if credit_id:
            bank_line = rows[credit_id]["description"]
            saved = query("""SELECT a.alias_norm FROM payer_aliases a JOIN tenancies t ON t.tenant_id = a.tenant_id
                             WHERE t.id = %s""", (int(r["tenancy_id"]),)) if r["tenancy_id"] else None
            names = [r["tenant_name"]] + ([] if saved is None or saved.empty else saved["alias_norm"].tolist())
            if not any(name_in_text(n, bank_line) for n in names):
                k = f"{rid}_{credit_id}"
                if st.checkbox(f"Save the payer's name for {r['tenant_name']}, so their next payment under this exact "
                               "name is checked automatically", value=True, key=f"al{k}"):
                    remember = st.text_input("Exact name to save (every word, as the bank prints it)",
                                             value=payer_text(bank_line), key=f"an{k}").strip() or None
        note = st.text_input("Note (needed to reject)", key=f"nt{rid}")
        b1, b2, b3 = st.columns(3)
        if b1.button("✓ Confirm payment", key=f"ok{rid}", type="primary"):
            try:
                with pool().connection() as conn:
                    msg = rc.confirm_receipt(conn, rid, user["id"], bank_txn_id=int(credit_id) if credit_id else None,
                                             amount=amount, paid_date=paid, remember_payer=remember,
                                             note=note.strip() or None)
                st.toast(msg); st.rerun()
            except ValueError as e:
                st.error(str(e))
        if b2.button("Reject slip", key=f"rj{rid}", disabled=not note.strip()):
            with pool().connection() as conn:
                rc.reject_receipt(conn, rid, user["id"], note.strip())
            st.rerun()
        if b3.button("Check again", key=f"rc{rid}"):
            with pool().connection() as conn:
                d = rc.match_receipt(conn, rid, user["id"])
            st.toast("Checked with the bank and recorded." if d.status == "matched" else "Still needs review.")
            st.rerun()


SLIPS_SQL = """SELECT r.*, tn.name AS tenant_name, u.code AS unit_code, rs.period
               FROM receipts r
               LEFT JOIN tenancies t ON t.id = r.tenancy_id
               LEFT JOIN tenants tn ON tn.id = t.tenant_id
               LEFT JOIN units u ON u.id = t.unit_id
               LEFT JOIN rent_schedule rs ON rs.id = r.rent_schedule_id"""


# ── Slip panel for one tenant's rent month (Rent Board) ──────────────────────
def bank_section(line: dict, user, hints):
    """Money in the bank for this tenant without a slip: what was recorded automatically, and what needs a click."""
    sid = int(line["schedule_id"])
    auto = query("""SELECT p.id, p.amount, p.paid_date, b.description FROM payments p
                    LEFT JOIN bank_transactions b ON b.id = p.bank_txn_id
                    WHERE p.rent_schedule_id = %s AND p.auto_from_bank AND NOT p.voided""", (sid,))
    for p in auto.to_dict("records"):
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**🏦 Recorded from the bank** (no slip) · RM{float(p['amount']):,.2f} on "
                        f"{p['paid_date']:%d/%m/%Y} · `{p['description'] or ''}`")
            if _can_edit(user) and c2.button("Undo", key=f"ub{p['id']}"):
                with pool().connection() as conn:
                    rc.undo_bank_payment(conn, int(p["id"]), user["id"])
                st.rerun()
    for credit, pick in hints or []:
        with st.container(border=True):
            st.markdown(f"**💰 In the bank under this tenant's name, not recorded yet** · "
                        f"RM{float(credit['amount']):,.2f} on {credit['txn_date']:%d/%m/%Y} · "
                        f"`{credit['description']}`")
            if pick.reason:
                st.caption(pick.reason)
            elif credit["no_auto"]:
                st.caption("Undone earlier, so it isn't recorded automatically.")
            if not _can_edit(user):
                continue
            with pool().connection() as conn:
                split = rc.split_for(conn, credit, int(line["tenancy_id"]))
            label = " + ".join(f"{ln['period']:%b %Y} RM{amt:,.2f}" for ln, amt in split)
            if split and st.button(f"✓ Record: {label}", key=f"rb{credit['id']}_{sid}",
                                   help="Oldest unpaid month first; anything extra goes on the latest month"):
                with pool().connection() as conn:
                    ok = rc.record_bank_credit(conn, int(credit["id"]), int(line["tenancy_id"]), user["id"])
                st.session_state[f"flash{sid}"] = ("ok", "Payment recorded.") if ok else \
                    ("info", "That bank credit was already used.")
                st.rerun()


def slip_panel(line: dict, user, hints=None):
    """line: a v_rent_status row (schedule_id, tenancy_id, unit_code, tenant_name, period, balance, …);
    hints: this tenancy's bank credits not recorded yet (rc.bank_hints)."""
    sid, tid = int(line["schedule_id"]), int(line["tenancy_id"])
    st.markdown(f"#### {line['unit_code']} · {line['tenant_name']} · {line['period']:%B %Y}")
    st.caption(f"Total RM{float(line['total_due']):,.2f} ({breakdown(line)}) · paid RM{float(line['paid']):,.2f} · "
               f"balance RM{float(line['balance']):,.2f} · due {line['due_date']:%d/%m/%Y} · grace until "
               f"{line['grace_end']:%d/%m/%Y} · {line['status']}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning("AI reading isn't set up: add ANTHROPIC_API_KEY to .env / Streamlit secrets.")
    flash = st.session_state.pop(f"flash{sid}", None)
    if flash and flash[0] == "ok":
        st.success(flash[1])
    elif flash:
        st.info(flash[1])

    bank_section(line, user, hints)
    slips = query(SLIPS_SQL + " WHERE r.rent_schedule_id = %s ORDER BY r.uploaded_at DESC", (sid,))
    for r in slips.to_dict("records"):
        with st.container(border=True):
            st.markdown(f"**{STATUS[r['status']]}** · slip uploaded {r['uploaded_at']:%d %b %H:%M}")
            if r["status"] == "review":
                review_card(r, user)
            elif r["status"] in ("matched", "confirmed") and _can_edit(user):
                c1, c2 = st.columns([1, 2])
                with c1:
                    show_file(r["file_path"], width=160)
                c2.caption("Undo puts the slip back to review; a payment it created is voided (kept, with a reason).")
                if c2.button("Undo", key=f"undo{r['id']}"):
                    with pool().connection() as conn:
                        rc.undo_receipt(conn, int(r["id"]), user["id"])
                    st.rerun()
            elif r["flags"]:
                st.caption(" · ".join(r["flags"]))

    if _can_edit(user):
        up = st.file_uploader(f"📎 Upload {line['tenant_name']}'s transfer slip", key=f"up{sid}",
                              type=["jpg", "jpeg", "png", "webp", "pdf"])
        if up and st.button("Check slip with the bank", type="primary", key=f"go{sid}"):
            try:
                with st.spinner("Reading the slip and checking the bank statement…"), pool().connection() as conn:
                    res = rc.process_slip(conn, up.getvalue(), up.name, user["id"], tenancy_id=tid, schedule_id=sid)
            except AIError as e:
                st.error(str(e)); return
            st.session_state[f"flash{sid}"] = (
                ("ok", "✅ The bank shows this payment under the tenant's name: recorded.")
                if res["status"] == "matched" else
                ("info", res["message"] if res["status"] in ("duplicate", "not_slip")
                 else "🟡 Couldn't confirm it with the bank: see the reasons below."))
            st.rerun()
