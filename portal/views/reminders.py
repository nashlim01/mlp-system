import pandas as pd
import streamlit as st

from db import execute, query
from messages import fill, wa_link

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
            st.warning("No phone number on record: update the tenant in Register.")
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

with rent_tab:
    f = "AND r.assigned_staff_id = %(sid)s" if mine else ""
    rows = query(f"""
        SELECT r.schedule_id, r.tenancy_id, r.unit_code, r.tenant_name, r.tenant_phone,
               r.period, r.balance, r.due_date, r.status, r.days_late, lr.last_reminded_at
        FROM v_rent_status r
        LEFT JOIN v_last_reminder lr ON lr.tenancy_id = r.tenancy_id
        WHERE r.status IN ('OVERDUE', 'DUE', 'PARTIAL')
          AND r.due_date <= today_myt() + 3 {f}
        ORDER BY r.status = 'OVERDUE' DESC, r.days_late DESC, r.due_date""", p)
    if rows.empty:
        st.success("No rent reminders needed right now.")
    for r in rows.itertuples():
        late = f" ({r.days_late} days late)" if r.days_late else ""
        reminder_row(
            key=f"r{r.schedule_id}",
            header=f"**{r.unit_code}** · {r.tenant_name} · RM{r.balance:,.2f} · "
                   f"{r.status}{late} · _{last_text(r.last_reminded_at)}_",
            default_code="rent_overdue" if r.status == "OVERDUE" else "rent_reminder",
            values=dict(tenant_name=r.tenant_name, staff_name=user["name"], unit_code=r.unit_code,
                        period=f"{r.period:%B %Y}", balance=f"{r.balance:,.2f}",
                        due_date=f"{r.due_date:%d/%m/%Y}"),
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
