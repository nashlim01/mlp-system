import datetime as dt

import streamlit as st

from db import audit, execute, pool, query
from storage import receipt_link

user = st.session_state.user
can_edit = user["role"] != "viewer"
st.title("Tenancy")

mine = st.toggle("Only my units", value=user["role"] != "admin")
f = "WHERE u.assigned_staff_id = %(sid)s" if mine else ""
units = query(f"""SELECT u.id, u.code, u.address, u.status, COALESCE(s.name, 'Unassigned') AS staff
                  FROM units u LEFT JOIN staff s ON s.id = u.assigned_staff_id
                  {f} ORDER BY u.code""", {"sid": user["id"]})
if units.empty:
    st.info("No units yet. Import the register or add units in Register."); st.stop()
labels = [f"{r.code} · {r.address}" for r in units.itertuples()]
i = st.selectbox("Unit", range(len(labels)), format_func=lambda k: labels[k])
unit = units.iloc[i]
unit_id, unit_code = int(unit["id"]), unit["code"]

tenancies = query("""SELECT t.*, tn.name AS tenant_name, tn.phone AS tenant_phone
                     FROM tenancies t JOIN tenants tn ON tn.id = t.tenant_id
                     WHERE t.unit_id = %s
                     ORDER BY t.status = 'active' DESC, t.start_date DESC""", (unit_id,))
if tenancies.empty:
    st.info(f"{unit_code} has no tenancies ({unit['status']}). Start one in Register."); st.stop()
if len(tenancies) > 1:
    k = st.selectbox("Tenancy", range(len(tenancies)), format_func=lambda k: (
        f"{tenancies.iloc[k]['tenant_name']} · from {tenancies.iloc[k]['start_date']:%d/%m/%Y}"
        f" · {tenancies.iloc[k]['status']}"))
else:
    k = 0
t = tenancies.iloc[k]
tenancy_id = int(t["id"])

# ── Details ──────────────────────────────────────────────────────────────────
st.subheader(f"{unit_code} · {t['tenant_name']}")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Monthly rent", f"RM{t['monthly_rent']:,.2f}")
c2.metric("Due day", int(t["due_day"]))
c3.metric("Deposit (rental)", f"RM{t['deposit_rental'] or 0:,.2f}")
c4.metric("Deposit (utility)", f"RM{t['deposit_utility'] or 0:,.2f}")
end = f"{t['end_date']:%d/%m/%Y}" if t["end_date"] else "open-ended"
st.write(f"**Status:** {t['status']} · **Period:** {t['start_date']:%d/%m/%Y} to {end} · "
         f"**Phone:** {t['tenant_phone'] or '—'} · **Assigned to:** {unit['staff']}")
if t["notes"]:
    st.caption(t["notes"])

# ── Monthly rent ───────────────────────────────────────────────────────────────
st.subheader("Rent (last 12 months)")
lines = query("""SELECT schedule_id, period, amount_due, paid, balance, due_date, status,
                        days_late, note
                 FROM v_rent_status WHERE tenancy_id = %s
                 ORDER BY period DESC LIMIT 12""", (tenancy_id,))
if lines.empty:
    st.info("No monthly rent prepared yet.")
else:
    icons = {"PAID": "🟢", "PARTIAL": "🟠", "DUE": "🟡", "OVERDUE": "🔴"}
    show = lines.drop(columns="schedule_id").copy()
    show["period"] = show["period"].map(lambda d: f"{d:%b %Y}")
    show["status"] = show["status"].map(lambda s: f"{icons[s]} {s}")
    st.dataframe(show, hide_index=True, width="stretch")

    if can_edit:
        with st.expander("Change a month's rent amount (e.g. pro-rata first or last month)"):
            rows = list(lines.itertuples())
            j = st.selectbox("Month", range(len(rows)), key="adj_month",
                             format_func=lambda j: f"{rows[j].period:%b %Y} (RM{rows[j].amount_due:,.2f})")
            row = rows[j]
            with st.form("adjust"):
                new_amount = st.number_input("New amount due (RM)", min_value=0.01,
                                             value=float(row.amount_due), step=10.0)
                reason = st.text_input("Reason (required)")
                if st.form_submit_button("Save adjustment"):
                    if not reason.strip():
                        st.error("Give a reason for the adjustment.")
                    elif abs(new_amount - float(row.amount_due)) < 0.005:
                        st.info("Amount unchanged.")
                    else:
                        with pool().connection() as conn, conn.transaction():
                            n = conn.execute("""UPDATE rent_schedule
                                                SET amount_due = %s,
                                                    note = concat_ws(' · ', note, %s)
                                                WHERE id = %s AND amount_due = %s""",
                                             (new_amount, f"Adjusted: {reason.strip()}",
                                              int(row.schedule_id), row.amount_due)).rowcount
                            if n:
                                audit(conn, user["id"], "adjust_rent_line", "rent_schedule",
                                      int(row.schedule_id), {"amount_due": row.amount_due},
                                      {"amount_due": new_amount, "reason": reason.strip()})
                        if n:
                            st.success("Rent amount updated."); st.rerun()
                        else:
                            st.warning("Someone changed this line just now. Reload and try again.")

# ── Payments ─────────────────────────────────────────────────────────────────
st.subheader("Payments")
pays = query("""SELECT p.id, rs.period, p.amount, p.paid_date, p.method, p.reference,
                       p.receipt_path, p.voided, p.void_reason,
                       COALESCE(s.name, 'import') AS recorded_by,
                       (p.bank_txn_id IS NOT NULL) AS bank_matched
                FROM payments p
                LEFT JOIN rent_schedule rs ON rs.id = p.rent_schedule_id
                LEFT JOIN staff s ON s.id = p.recorded_by
                WHERE p.tenancy_id = %s ORDER BY p.paid_date DESC, p.id DESC""", (tenancy_id,))
if pays.empty:
    st.info("No payments recorded.")
for p in pays.itertuples():
    month = f"{p.period:%b %Y}" if p.period else "-"
    label = (f"{month} · RM{p.amount:,.2f} · {p.paid_date:%d/%m/%Y} · {p.method}"
             f"{' · ' + p.reference if p.reference else ''} · by {p.recorded_by}"
             f"{' · 🏦 bank-matched' if p.bank_matched else ''}")
    c1, c2 = st.columns([6, 1])
    if p.voided:
        c1.caption(f"~~{label}~~ (voided: {p.void_reason})")
    else:
        c1.write(label)
    if p.receipt_path and c2.button("Receipt", key=f"rc{p.id}"):
        try:
            st.link_button(f"Open receipt for {month} (link valid 5 minutes)",
                           receipt_link(p.receipt_path))
        except Exception as e:                       # storage not configured / file missing
            st.error(f"Could not open the receipt: {e}")

# ── Reminders ────────────────────────────────────────────────────────────────
st.subheader("WhatsApp reminders sent")
rem = query("""SELECT r.created_at AT TIME ZONE 'Asia/Kuching' AS sent_at,
                      COALESCE(s.name, '-') AS staff, r.template_code, r.message
               FROM reminder_log r LEFT JOIN staff s ON s.id = r.staff_id
               WHERE r.tenancy_id = %s ORDER BY r.created_at DESC LIMIT 20""", (tenancy_id,))
if rem.empty:
    st.caption("No reminders logged.")
else:
    st.dataframe(rem, hide_index=True, width="stretch")

# ── Follow-ups ───────────────────────────────────────────────────────────────
st.subheader("Follow-ups")
fus = query("""SELECT fo.id, fo.note, fo.next_action_date, fo.done, COALESCE(s.name, '-') AS staff,
                      fo.created_at AT TIME ZONE 'Asia/Kuching' AS created
               FROM followups fo LEFT JOIN staff s ON s.id = fo.staff_id
               WHERE fo.tenancy_id = %s ORDER BY fo.done, fo.next_action_date""", (tenancy_id,))
for r in fus.itertuples():
    c1, c2 = st.columns([6, 1])
    when = f"{r.next_action_date:%d/%m/%Y}" if r.next_action_date else "no date"
    text = f"**{when}**: {r.note} _({r.staff}, {r.created:%d %b})_"
    c1.markdown(f"~~{text}~~" if r.done else text)
    if not r.done and can_edit and c2.button("Done", key=f"fud{r.id}"):
        execute("UPDATE followups SET done = TRUE WHERE id = %s", (int(r.id),))
        st.rerun()
if can_edit:
    with st.form("followup", clear_on_submit=True):
        note = st.text_input("New follow-up note", placeholder="Called tenant, will pay Friday")
        next_date = st.date_input("Next action date", value=dt.date.today() + dt.timedelta(days=3),
                                  format="DD/MM/YYYY")
        open_lines = lines[lines["balance"] > 0] if not lines.empty else lines
        line_opts = [None] + (open_lines["schedule_id"].tolist() if not open_lines.empty else [])
        periods = dict(zip(lines["schedule_id"], lines["period"])) if not lines.empty else {}
        sid = st.selectbox("About rent for", line_opts,
                           format_func=lambda v: "General" if v is None else f"{periods[v]:%b %Y}")
        if st.form_submit_button("Add follow-up"):
            if not note.strip():
                st.error("Write a note first.")
            else:
                execute("""INSERT INTO followups (tenancy_id, rent_schedule_id, staff_id, note,
                                                  next_action_date)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (tenancy_id, int(sid) if sid is not None else None, user["id"],
                         note.strip(), next_date))
                st.rerun()

# ── Electricity ──────────────────────────────────────────────────────────────
st.subheader("Electricity")
el = query("""SELECT account_no, amount_due, outstanding, due_date,
                     checked_at AT TIME ZONE 'Asia/Kuching' AS checked_at
              FROM v_electric_latest WHERE unit_code = %s""", (unit_code,))
if el.empty:
    st.caption("No electricity account linked to this unit.")
else:
    st.dataframe(el, hide_index=True, width="stretch")
