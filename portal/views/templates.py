import re

import psycopg
import streamlit as st

from db import audit, pool, query
from messages import fill

user = st.session_state.user
can_edit = user["role"] != "viewer"
st.title("Message Templates")
st.caption("The WhatsApp messages offered in the Reminder Queue and after recording a payment. "
           "Staff pick one, can still edit the text before sending, and press Send in WhatsApp themselves.")

PLACEHOLDERS = {
    "{tenant_name}": "Tenant's name", "{staff_name}": "Your name", "{unit_code}": "Unit, e.g. 2273 SENADIN",
    "{period}": "Rent month, e.g. October 2026", "{balance}": "Amount still owed", "{due_date}": "Due date",
    "{amount}": "Amount just paid (thank-you)", "{end_date}": "Tenancy end date (tenancy ending)",
}
SAMPLE = dict(tenant_name="Sumerni", staff_name=user["name"], unit_code="2273 SENADIN", period="October 2026",
              balance="850.00", due_date="15/10/2026", amount="850.00", end_date="14/03/2027")

tpls = query("SELECT code, name, body, active FROM message_templates ORDER BY active DESC, code")
st.dataframe(tpls[["name", "code", "active"]], hide_index=True, width="stretch",
             column_config={"name": "Template", "code": "Code",
                            "active": st.column_config.CheckboxColumn("Shown in Reminder Queue")})

NEW = "➕ New template"
codes = ([NEW] if can_edit else []) + tpls["code"].tolist()
names = dict(zip(tpls["code"], tpls["name"]))
code = st.selectbox("Template", codes, index=1 if can_edit and len(codes) > 1 else 0,
                    format_func=lambda c: c if c == NEW else f"{names[c]} ({c})")
t = None if code == NEW else tpls.set_index("code").loc[code].to_dict()

left, right = st.columns([3, 2])
with left:
    new_code = st.text_input("Code (letters, digits, _), e.g. rent_overdue_bm",
                             value="" if t is None else code, disabled=t is not None or not can_edit,
                             key=f"code_{code}")
    name = st.text_input("Name shown to staff", value=(t or {}).get("name", ""), disabled=not can_edit,
                         key=f"name_{code}")
    body = st.text_area("Message", value=(t or {}).get("body", ""), height=220, disabled=not can_edit,
                        key=f"body_{code}", max_chars=1000)
    active = st.checkbox("Show in the Reminder Queue", value=bool((t or {}).get("active", True)),
                         disabled=not can_edit, key=f"active_{code}")
    if can_edit and st.button("Save template", type="primary"):
        c = (new_code if t is None else code).strip().lower()
        if not c.replace("_", "").isalnum() or not name.strip() or not body.strip():
            st.error("Code (letters, digits, _), name and message are required.")
        else:
            try:
                with pool().connection() as conn, conn.transaction():
                    if t is None:
                        conn.execute("""INSERT INTO message_templates (code, name, body, active)
                                        VALUES (%s, %s, %s, %s)""", (c, name.strip(), body, active))
                    else:
                        conn.execute("""UPDATE message_templates SET name = %s, body = %s, active = %s
                                        WHERE code = %s""", (name.strip(), body, active, c))
                    audit(conn, user["id"], "add_template" if t is None else "edit_template",
                          "message_templates", None, t and {**t, "code": c},
                          {"code": c, "name": name.strip(), "body": body, "active": active})
                st.success("Template saved."); st.rerun()
            except psycopg.errors.UniqueViolation:
                st.error(f"Template code '{c}' already exists.")

with right:
    st.markdown("**Preview** (sample values, updates as you type)")
    with st.container(border=True):
        st.text(fill(body, **SAMPLE) if body.strip() else "…")
    st.markdown("**Placeholders you can use**")
    st.dataframe([{"Placeholder": k, "Becomes": v} for k, v in PLACEHOLDERS.items()],
                 hide_index=True, width="stretch")
    unknown = set(re.findall(r"\{[a-z_]+\}", body)) - set(PLACEHOLDERS)
    if unknown:
        st.warning(f"Not a known placeholder (will be sent as typed): {', '.join(sorted(unknown))}")
