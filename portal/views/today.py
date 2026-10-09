import streamlit as st

from db import execute, query

user = st.session_state.user
st.title("Today")
mine = st.toggle("Only my units", value=user["role"] != "admin")
f = "AND assigned_staff_id = %(sid)s" if mine else ""
fu = "AND u.assigned_staff_id = %(sid)s" if mine else ""
p = {"sid": user["id"]}


def show(title, df, empty_msg):
    st.subheader(title)
    if df.empty:
        st.success(empty_msg)
    else:
        st.dataframe(df, hide_index=True, width="stretch")


st.caption("🔴 = grace period over: no more WhatsApp reminders. Settle with the tenant in person.")
show("🔴 Overdue rent (grace period over)", query(f"""
    SELECT r.unit_code, r.tenant_name, r.period, r.balance, r.days_late,
           lr.last_reminded_at, r.staff_name
    FROM v_rent_status r
    LEFT JOIN v_last_reminder lr ON lr.tenancy_id = r.tenancy_id
    WHERE r.status = 'OVERDUE' {f.replace('assigned', 'r.assigned')}
    ORDER BY r.days_late DESC""", p), "Nothing overdue.")
st.page_link("views/reminders.py", label="Send WhatsApp reminders →", icon="💬")

slips = execute("SELECT count(*) AS n FROM receipts WHERE status = 'review'")
if slips and slips["n"]:
    st.page_link("views/receipts.py", label=f"🧾 {slips['n']} transfer slip(s) need review →")

show("🟡 Due within 3 days, or in the grace period", query(f"""
    SELECT unit_code, tenant_name, period, balance, due_date, grace_end
    FROM v_rent_status
    WHERE status IN ('DUE', 'PARTIAL', 'GRACE') AND due_date <= today_myt() + 3 {f}
    ORDER BY due_date""", p), "Nothing due in the next 3 days.")

st.subheader("📝 Follow-ups due")
fus = query(f"""
    SELECT fo.id, u.code AS unit_code, fo.note, fo.next_action_date
    FROM followups fo
    JOIN tenancies t ON t.id = fo.tenancy_id
    JOIN units u ON u.id = t.unit_id
    WHERE NOT fo.done AND fo.next_action_date <= today_myt() {fu}
    ORDER BY fo.next_action_date""", p)
if fus.empty:
    st.success("No follow-ups due.")
for r in fus.itertuples():
    c1, c2 = st.columns([6, 1])
    c1.write(f"**{r.unit_code}** ({r.next_action_date}): {r.note}")
    if c2.button("Done", key=f"fu{r.id}"):
        execute("UPDATE followups SET done = TRUE WHERE id = %s", (int(r.id),))
        st.rerun()

show("📄 Leases ending within 60 days", query(f"""
    SELECT unit_code, tenant_name, end_date, days_left FROM v_lease_expiry
    WHERE TRUE {f} ORDER BY end_date""", p), "No leases ending soon.")

show("⚡ Electricity overdue", query(f"""
    SELECT unit_code, account_no, outstanding, due_date, checked_at FROM v_electric_latest
    WHERE outstanding > 0 AND due_date < today_myt() {f}
    ORDER BY due_date""", p), "No overdue electricity bills.")

if user["role"] == "admin":
    st.subheader("⚙️ System health")
    failed = query("""SELECT job, started_at AT TIME ZONE 'Asia/Kuching' AS started,
                             left(error, 300) AS error
                      FROM job_runs WHERE status = 'failed'
                        AND started_at > NOW() - INTERVAL '48 hours'
                      ORDER BY started_at DESC""")
    if failed.empty:
        st.success("All scheduled jobs ran OK in the last 48 hours.")
    else:
        st.error("Some jobs failed:")
        st.dataframe(failed, hide_index=True, width="stretch")
    last_ok = execute("""SELECT max(finished_at) AS t FROM job_runs
                         WHERE job = 'electric_check' AND status = 'ok'""")
    if last_ok and last_ok["t"] is None:
        st.info("The electricity check has not completed successfully yet.")
