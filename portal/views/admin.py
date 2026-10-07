import datetime as dt

import psycopg
import streamlit as st

from db import audit, pool, query
from messages import fill, norm_phone

user = st.session_state.user
if user["role"] != "admin":
    st.error("Admins only."); st.stop()
st.title("Admin")

ROLES = ["admin", "staff", "viewer"]
SAMPLE = dict(tenant_name="Ahmad", staff_name=user["name"], unit_code="SNDN-3418", period="October 2026",
              balance="1,200.00", due_date="07/10/2026", amount="1,200.00", end_date="31/12/2026")

staff_tab, tpl_tab, bank_tab, rent_tab, log_tab = st.tabs(
    ["Staff", "Message templates", "Landlord bank details", "Rent lines", "Jobs & audit log"])

# ── Staff ────────────────────────────────────────────────────────────────────
with staff_tab:
    staff = query("""SELECT id, name, email, phone, role, active, password_hash IS NOT NULL AS has_password
                     FROM staff ORDER BY active DESC, name""")
    if not staff.empty:
        st.dataframe(staff.drop(columns="id"), hide_index=True, width="stretch")
    st.caption("Passwords are set with `python scripts/set_password.py`. Staff also need their email "
               "added to the Cloudflare Access policy.")
    opts = ["➕ New"] + staff["id"].tolist() if not staff.empty else ["➕ New"]
    names = dict(zip(staff["id"], staff["name"] + " · " + staff["email"])) if not staff.empty else {}
    sid = st.selectbox("Staff member", opts, format_func=lambda v: names.get(v, v))
    s = staff[staff["id"] == sid].iloc[0].to_dict() if sid in names else None
    with st.form(f"staff_{sid}"):
        name = st.text_input("Name", value=(s or {}).get("name") or "")
        email = st.text_input("Email (login)", value=(s or {}).get("email") or "", disabled=s is not None)
        phone = st.text_input("Phone", value=(s or {}).get("phone") or "")
        role = st.selectbox("Role", ROLES, index=ROLES.index(s["role"]) if s else 1)
        active = st.checkbox("Active (can log in)", value=bool(s["active"]) if s else True)
        if st.form_submit_button("Save"):
            after = {"name": name.strip(), "phone": norm_phone(phone), "role": role, "active": active}
            if not after["name"] or (s is None and not email.strip()):
                st.error("Name and email are required.")
            elif s and s["id"] == user["id"] and (role != "admin" or not active):
                st.error("You can't remove your own admin access.")
            else:
                try:
                    with pool().connection() as conn, conn.transaction():
                        if s is None:
                            new_id = conn.execute("""INSERT INTO staff (name, email, phone, role, active)
                                                     VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                                                  (after["name"], email.strip().lower(), after["phone"],
                                                   role, active)).fetchone()["id"]
                            audit(conn, user["id"], "add_staff", "staff", new_id, None,
                                  {**after, "email": email.strip().lower()})
                        else:
                            conn.execute("""UPDATE staff SET name = %s, phone = %s, role = %s, active = %s
                                            WHERE id = %s""",
                                         (after["name"], after["phone"], role, active, int(s["id"])))
                            audit(conn, user["id"], "edit_staff", "staff", int(s["id"]),
                                  {k: s[k] for k in after}, after)
                    st.success("Saved."); st.rerun()
                except psycopg.errors.UniqueViolation:
                    st.error("A staff member with this email already exists.")

# ── Message templates ────────────────────────────────────────────────────────
with tpl_tab:
    tpls = query("SELECT code, name, body, active FROM message_templates ORDER BY code")
    st.caption("Placeholders: {tenant_name} {staff_name} {unit_code} {period} {balance} {due_date} "
               "{amount} {end_date}. Add language versions as new templates, e.g. rent_overdue_bm.")
    codes = ["➕ New"] + tpls["code"].tolist()
    code = st.selectbox("Template", codes, format_func=lambda c: c if c == "➕ New" else
                        f"{c} · {tpls.set_index('code').loc[c, 'name']}")
    t = tpls.set_index("code").loc[code].to_dict() if code != "➕ New" else None
    with st.form(f"tpl_{code}"):
        new_code = st.text_input("Code (letters, digits, _)", value="" if t is None else code,
                                 disabled=t is not None)
        name = st.text_input("Name", value=(t or {}).get("name", ""))
        body = st.text_area("Message", value=(t or {}).get("body", ""), height=180)
        active = st.checkbox("Active (shown in the Reminder Queue)", value=bool((t or {}).get("active", True)))
        if st.form_submit_button("Save template"):
            c = (new_code if t is None else code).strip().lower()
            if not c.replace("_", "").isalnum() or not name.strip() or not body.strip():
                st.error("Code (letters, digits, _), name and message are required.")
            elif len(body) > 1000:
                st.error("Keep messages under 1,000 characters so the WhatsApp link works.")
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
    if t:
        st.markdown("**Preview with sample values**")
        st.text(fill(t["body"], **SAMPLE))

# ── Landlord bank details (admin only) ───────────────────────────────────────
with bank_tab:
    lls = query("""SELECT l.id, l.code, l.name, b.bank, b.account_name, b.account_no
                   FROM landlords l LEFT JOIN landlord_bank b ON b.landlord_id = l.id ORDER BY l.code""")
    if lls.empty:
        st.info("No landlords yet.")
    else:
        lid = st.selectbox("Landlord", lls["id"].tolist(),
                           format_func=dict(zip(lls["id"], lls["code"] + " · " + lls["name"])).get)
        b = lls[lls["id"] == lid].iloc[0].to_dict()
        with st.form(f"bank_{lid}"):
            bank = st.text_input("Bank", value=b["bank"] or "")
            acc_name = st.text_input("Account name", value=b["account_name"] or "")
            acc_no = st.text_input("Account number", value=b["account_no"] or "")
            if st.form_submit_button("Save bank details"):
                mask = lambda v: f"…{v[-4:]}" if v else None      # audit keeps the last 4 digits only
                with pool().connection() as conn, conn.transaction():
                    conn.execute("""INSERT INTO landlord_bank (landlord_id, bank, account_name, account_no)
                                    VALUES (%s, %s, %s, %s)
                                    ON CONFLICT (landlord_id) DO UPDATE
                                    SET bank = EXCLUDED.bank, account_name = EXCLUDED.account_name,
                                        account_no = EXCLUDED.account_no""",
                                 (int(lid), bank.strip() or None, acc_name.strip() or None,
                                  acc_no.strip() or None))
                    audit(conn, user["id"], "edit_landlord_bank", "landlord_bank", int(lid),
                          {"bank": b["bank"], "account_no": mask(b["account_no"])},
                          {"bank": bank.strip(), "account_no": mask(acc_no.strip())})
                st.success("Saved."); st.rerun()

# ── Rent lines ───────────────────────────────────────────────────────────────
with rent_tab:
    st.write("Create the rent lines for a month (one per active tenancy). Safe to run again: "
             "existing lines are not touched. The worker does this automatically on the 25th and the 1st.")
    today = dt.date.today().replace(day=1)
    months = [today] + [(today + dt.timedelta(days=32 * k)).replace(day=1) for k in (1, 2)] \
        + [(today - dt.timedelta(days=1)).replace(day=1)]
    month = st.selectbox("Month", months, format_func=lambda d: f"{d:%B %Y}")
    if st.button("Generate rent lines"):
        with pool().connection() as conn, conn.transaction():
            n = conn.execute("SELECT generate_rent_schedule(%s) AS n", (month,)).fetchone()["n"]
            audit(conn, user["id"], "generate_rent_schedule", "rent_schedule", None, None,
                  {"period": month, "created": n})
        st.success(f"{n} new rent line(s) created for {month:%B %Y}. Check lines flagged "
                   "'check pro-rata' on the Rent Board.")

# ── Jobs & audit ─────────────────────────────────────────────────────────────
with log_tab:
    st.subheader("Scheduled jobs (last 50)")
    st.dataframe(query("""SELECT job, started_at AT TIME ZONE 'Asia/Kuching' AS started,
                                 finished_at - started_at AS took, status, rows_affected,
                                 left(error, 300) AS error
                          FROM job_runs ORDER BY started_at DESC LIMIT 50"""),
                 hide_index=True, width="stretch")
    st.subheader("Audit log (last 200)")
    st.dataframe(query("""SELECT a.created_at AT TIME ZONE 'Asia/Kuching' AS at,
                                 COALESCE(s.name, '-') AS staff, a.action, a.entity, a.entity_id,
                                 a.before::text AS before, a.after::text AS after
                          FROM audit_log a LEFT JOIN staff s ON s.id = a.staff_id
                          ORDER BY a.created_at DESC LIMIT 200"""),
                 hide_index=True, width="stretch")
