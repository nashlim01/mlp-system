import pandas as pd
import streamlit as st

from db import execute, query
from messages import breakdown, fill, wa_link

user = st.session_state.user
st.title("Reminder Queue")
st.caption("💬 opens WhatsApp with the message ready → press Send in WhatsApp → click ✓ Sent here.")
mine = st.toggle("Only my units", value=user["role"] != "admin")
p = {"sid": user["id"]}

tpls = query("SELECT code, name, body FROM message_templates WHERE active ORDER BY name")
bodies = dict(zip(tpls["code"], tpls["body"]))
names = dict(zip(tpls["code"], tpls["name"]))
codes = list(bodies)


def last_text(ts):
    if ts is None or pd.isna(ts):
        return "not reminded yet"
    today = pd.Timestamp.now(tz="Asia/Kuching").date()
    return "⚠️ reminded today" if ts.date() == today else f"last reminded {ts:%d %b, %H:%M}"


def reminder_row(key, header, default_code, values, tenancy_id, schedule_id, phone):
    with st.container(border=True):
        st.markdown(header)
        if not phone:
            st.warning("No phone number on record: update the tenant under Personnel → Tenants.")
            return
        code = st.selectbox("Template", codes, index=codes.index(default_code) if default_code in codes else 0,
                            format_func=lambda c: names[c], key=f"t{key}")
        msg = st.text_area("Message", fill(bodies[code], **values), key=f"m{key}_{code}", height=120)
        c1, c2 = st.columns(2)
        c1.link_button("💬 Open in WhatsApp", wa_link(phone, msg))
        if c2.button("✓ Sent", key=f"s{key}"):
            execute("""INSERT INTO reminder_log (tenancy_id, rent_schedule_id, staff_id,
                                                 template_code, message)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (tenancy_id, schedule_id, user["id"], code, msg))
            st.toast("Reminder logged.")
            st.rerun()


rent_tab, lease_tab = st.tabs(["Rent", "Tenancies ending"])

STAGES = {"rent_reminder": "1️⃣ 1st reminder (due date)", "rent_grace_reminder": "2️⃣ 2nd reminder (in grace)",
          "rent_grace_end": "3️⃣ 3rd reminder (grace ends today)",
          "rent_balance": "➗ Remainder after a part payment"}

with rent_tab:
    st.caption("Each unpaid month shows the reminder due today: 1st on the due date, 2nd on day 5 of the 7-day "
               "grace period, 3rd on the day the grace period ends. After a part payment it asks for the remainder. "
               "Sending one (✓ Sent) takes it off the list until the next one is due. Once the grace period is over "
               "there are no more reminders: overdue rent is on Today for staff to settle with the tenant in person.")
    show_all = st.toggle("Show every unpaid month (not only reminders due now)", value=False)
    f = "AND r.assigned_staff_id = %(sid)s" if mine else ""
    due = "" if show_all else "AND r.stage IS NOT NULL"
    rows = query(f"""
        SELECT r.schedule_id, r.tenancy_id, r.unit_code, r.tenant_name, r.tenant_phone, r.period, r.paid,
               r.balance, r.due_date, r.grace_end, r.status, r.days_late, r.stage, lr.last_reminded_at,
               r.amount_due, r.electric_due, r.water_due
        FROM v_reminder_due r
        LEFT JOIN v_last_reminder lr ON lr.tenancy_id = r.tenancy_id
        WHERE r.status NOT IN ('PAID', 'OVERDUE') AND r.due_date <= today_myt() {due} {f}
        ORDER BY r.status = 'OVERDUE' DESC, r.days_late DESC, r.due_date""", p)
    if rows.empty:
        st.success("No rent reminders needed right now.")
    for r in rows.itertuples():
        late = f" ({r.days_late} days after due)" if r.days_late else ""
        stage = STAGES.get(r.stage, "no reminder due now")
        default = r.stage or {"GRACE": "rent_grace_reminder"}.get(r.status, "rent_reminder")
        reminder_row(
            key=f"r{r.schedule_id}",
            header=f"**{r.unit_code}** · {r.tenant_name} · {r.period:%b %Y} · RM{r.balance:,.2f} left · "
                   f"{r.status}{late} · **{stage}** · _{last_text(r.last_reminded_at)}_",
            default_code=default,
            values=dict(tenant_name=r.tenant_name, staff_name=user["name"], unit_code=r.unit_code,
                        period=f"{r.period:%B %Y}", balance=f"{r.balance:,.2f}", paid=f"{r.paid:,.2f}",
                        breakdown=breakdown(r),
                        due_date=f"{r.due_date:%d/%m/%Y}", grace_end=f"{r.grace_end:%d/%m/%Y}"),
            tenancy_id=int(r.tenancy_id), schedule_id=int(r.schedule_id), phone=r.tenant_phone)

with lease_tab:
    f = "AND e.assigned_staff_id = %(sid)s" if mine else ""
    rows = query(f"""
        SELECT e.tenancy_id, e.unit_code, e.tenant_name, e.end_date, e.days_left,
               tn.phone, lr.last_reminded_at
        FROM v_lease_expiry e
        JOIN tenancies t ON t.id = e.tenancy_id
        JOIN tenants tn ON tn.id = t.tenant_id
        LEFT JOIN v_last_reminder lr ON lr.tenancy_id = e.tenancy_id
        WHERE TRUE {f} ORDER BY e.end_date""", p)
    if rows.empty:
        st.success("No tenancies ending within 60 days.")
    for r in rows.itertuples():
        reminder_row(
            key=f"l{r.tenancy_id}",
            header=f"**{r.unit_code}** · {r.tenant_name} · ends {r.end_date:%d %b %Y} "
                   f"({r.days_left} days) · _{last_text(r.last_reminded_at)}_",
            default_code="lease_ending",
            values=dict(tenant_name=r.tenant_name, staff_name=user["name"], unit_code=r.unit_code,
                        end_date=f"{r.end_date:%d/%m/%Y}"),
            tenancy_id=int(r.tenancy_id), schedule_id=None, phone=r.phone)
