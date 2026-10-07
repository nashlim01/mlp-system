import datetime as dt

import streamlit as st

from db import audit, pool, query
from messages import fill, wa_link
from storage import upload_receipt

user = st.session_state.user
st.title("Record Payment")

units = query("""SELECT t.id AS tenancy_id, u.code, tn.name AS tenant, tn.phone
                 FROM tenancies t JOIN units u ON u.id = t.unit_id
                 JOIN tenants tn ON tn.id = t.tenant_id
                 WHERE t.status = 'active' ORDER BY u.code""")
if units.empty:
    st.info("No active tenancies."); st.stop()
labels = [f"{r.code} - {r.tenant}" for r in units.itertuples()]
idx = st.selectbox("Unit", range(len(labels)), format_func=lambda i: labels[i])
sel = units.iloc[idx]
tenancy_id, unit_code, phone = int(sel["tenancy_id"]), sel["code"], sel["phone"]

open_rows = query("""SELECT schedule_id, period, amount_due, paid, balance, due_date, status
                     FROM v_rent_status WHERE tenancy_id = %s AND balance > 0
                     ORDER BY period""", (tenancy_id,))
if open_rows.empty:
    st.success("Nothing outstanding for this unit.")
else:
    st.dataframe(open_rows.drop(columns="schedule_id"), hide_index=True)
    rows = list(open_rows.itertuples())
    k = st.selectbox("For month", range(len(rows)),
                     format_func=lambda i: f"{rows[i].period:%b %Y} (balance RM{rows[i].balance:,.2f})")
    row = rows[k]

    with st.form("pay"):                    # keep values if the duplicate warning shows
        amount = st.number_input("Amount (RM)", min_value=0.0, value=float(row.balance), step=10.0)
        paid_date = st.date_input("Paid on", value=dt.date.today(), format="DD/MM/YYYY")
        method = st.selectbox("Method", ["bank_transfer", "duitnow", "cash", "cheque", "other"])
        reference = st.text_input("Reference (bank ref / cheque no.)")
        receipt = st.file_uploader("Receipt (optional)", type=["jpg", "jpeg", "png", "pdf"])
        not_dup = st.checkbox("This is not a duplicate (only if warned)")
        submitted = st.form_submit_button("Save payment")

    if submitted:
        if amount <= 0:
            st.error("Amount must be more than 0."); st.stop()
        dup = query("""SELECT 1 FROM payments WHERE tenancy_id = %s AND amount = %s
                       AND paid_date = %s AND NOT voided""", (tenancy_id, amount, paid_date))
        if not dup.empty and not not_dup:
            st.warning("A payment with the same amount and date already exists for this unit. "
                       "Tick 'This is not a duplicate' and save again if it is genuine.")
            st.stop()
        path = upload_receipt(unit_code, row.period, receipt) if receipt else None
        with pool().connection() as conn, conn.transaction():
            pid = conn.execute("""
                INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date, method,
                                      reference, receipt_path, recorded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (tenancy_id, int(row.schedule_id), amount, paid_date, method,
                 reference or None, path, user["id"])).fetchone()["id"]
            audit(conn, user["id"], "record_payment", "payments", pid, None,
                  {"unit": unit_code, "period": str(row.period), "amount": amount,
                   "paid_date": str(paid_date), "method": method})
        st.success(f"Saved RM{amount:,.2f} for {unit_code}, {row.period:%b %Y}.")
        tpl = query("SELECT body FROM message_templates WHERE code = 'payment_thanks' AND active")
        if phone and not tpl.empty:
            msg = fill(tpl.iloc[0]["body"], tenant_name=sel["tenant"], unit_code=unit_code,
                       amount=f"{amount:,.2f}", period=f"{row.period:%B %Y}")
            st.link_button("💬 Send thank-you on WhatsApp", wa_link(phone, msg))

st.divider()
st.subheader("Recent payments (void a mistake)")
recent = query("""SELECT p.id, rs.period, p.amount, p.paid_date, p.method, p.reference,
                         s.name AS recorded_by, p.recorded_by AS recorder_id, p.voided
                  FROM payments p
                  LEFT JOIN rent_schedule rs ON rs.id = p.rent_schedule_id
                  LEFT JOIN staff s ON s.id = p.recorded_by
                  WHERE p.tenancy_id = %s ORDER BY p.recorded_at DESC LIMIT 10""", (tenancy_id,))
for r in recent.itertuples():
    month = f"{r.period:%b %Y}" if r.period else "-"
    label = f"{month} · RM{r.amount:,.2f} · {r.paid_date} · {r.recorded_by or 'import'}"
    if r.voided:
        st.caption(f"~~{label}~~ (voided)"); continue
    can_void = user["role"] == "admin" or r.recorder_id == user["id"]
    with st.expander(label):
        reason = st.text_input("Reason for voiding", key=f"why{r.id}")
        if st.button("Void payment", key=f"void{r.id}", disabled=not can_void or not reason):
            with pool().connection() as conn, conn.transaction():
                n = conn.execute("""UPDATE payments
                                    SET voided = TRUE, void_reason = %s, voided_by = %s,
                                        voided_at = NOW()
                                    WHERE id = %s AND NOT voided""",
                                 (reason, user["id"], int(r.id))).rowcount
                if n:
                    audit(conn, user["id"], "void_payment", "payments", int(r.id),
                          {"amount": float(r.amount)}, {"voided": True, "reason": reason})
            st.rerun()
