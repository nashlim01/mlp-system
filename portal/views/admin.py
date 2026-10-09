import datetime as dt

import psycopg
import streamlit as st

from auth import MIN_PW, hash_password, password_problem
from db import audit, pool, query
from messages import norm_phone

user = st.session_state.user
if user["role"] != "admin":
    st.error("Admins only."); st.stop()
st.title("Admin")

ROLES = ["admin", "staff", "viewer"]

requests = query("""SELECT id, name, email, phone, requested_at AT TIME ZONE 'Asia/Kuching' AS requested_at
                     FROM staff_requests WHERE status = 'pending' ORDER BY requested_at""")
req_tab, staff_tab, rent_tab, log_tab = st.tabs(
    [f"Access requests ({len(requests)})", "Staff", "Monthly rent", "Jobs & audit log"])
st.caption("Message templates and landlord details (including bank details) have their own pages in the menu.")

# ── Access requests ──────────────────────────────────────────────────────────
with req_tab:
    st.caption("People who asked for an account from the login page. Approving creates their login with "
               "the password they chose. Check you know the person before approving.")
    if requests.empty:
        st.success("No requests waiting.")
    for r in requests.itertuples():
        with st.container(border=True):
            st.markdown(f"**{r.name}** · {r.email} · {r.phone or 'no phone'} · "
                        f"requested {r.requested_at:%d %b %Y, %H:%M}")
            c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
            role = c1.selectbox("Role", ROLES, index=1, key=f"rr{r.id}", label_visibility="collapsed")
            note = c2.text_input("Note", key=f"rn{r.id}", placeholder="Note (optional, e.g. reason for rejecting)",
                                 label_visibility="collapsed")
            if c3.button("✓ Approve", key=f"ra{r.id}", type="primary"):
                try:
                    with pool().connection() as conn, conn.transaction():
                        req = conn.execute("""UPDATE staff_requests
                                              SET status = 'approved', decided_by = %s, decided_at = NOW(),
                                                  decision_note = %s
                                              WHERE id = %s AND status = 'pending'
                                              RETURNING name, email, phone, password_hash""",
                                           (user["id"], note.strip() or None, int(r.id))).fetchone()
                        if req:
                            sid = conn.execute("""INSERT INTO staff (name, email, phone, role, password_hash, active)
                                                  VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id""",
                                               (req["name"], req["email"], req["phone"], role,
                                                req["password_hash"])).fetchone()["id"]
                            audit(conn, user["id"], "approve_access", "staff", sid, None,
                                  {"name": req["name"], "email": req["email"], "role": role, "request_id": int(r.id)})
                    st.toast(f"{r.name} can now log in." if req else "Already handled by someone else.")
                    st.rerun()
                except psycopg.errors.UniqueViolation:
                    st.error("A staff member with this email already exists. Reject this request instead.")
            if c4.button("Reject", key=f"rj{r.id}"):
                with pool().connection() as conn, conn.transaction():
                    if conn.execute("""UPDATE staff_requests
                                       SET status = 'rejected', decided_by = %s, decided_at = NOW(),
                                           decision_note = %s
                                       WHERE id = %s AND status = 'pending'""",
                                    (user["id"], note.strip() or None, int(r.id))).rowcount:
                        audit(conn, user["id"], "reject_access", "staff_requests", int(r.id), None,
                              {"email": r.email, "note": note.strip() or None})
                st.rerun()
    with st.expander("Past requests"):
        st.dataframe(query("""SELECT q.name, q.email, q.status, q.decision_note,
                                     q.decided_at AT TIME ZONE 'Asia/Kuching' AS decided_at,
                                     s.name AS decided_by
                              FROM staff_requests q LEFT JOIN staff s ON s.id = q.decided_by
                              WHERE q.status <> 'pending' ORDER BY q.decided_at DESC LIMIT 50"""),
                     hide_index=True, width="stretch")

# ── Staff ────────────────────────────────────────────────────────────────────
with staff_tab:
    staff = query("""SELECT id, name, email, phone, role, active, password_hash IS NOT NULL AS has_password
                     FROM staff ORDER BY active DESC, name""")
    if not staff.empty:
        st.dataframe(staff.drop(columns="id"), hide_index=True, width="stretch")
    st.caption("New staff can also use 'Request access' on the login page. Everyone changes their own "
               "password under My account. Staff also need their email on the portal's viewer list "
               "(Streamlit sharing settings, or the Cloudflare Access policy).")
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
        if s is None:
            init_pw = st.text_input(f"Initial password (min {MIN_PW} characters; tell them to change it)",
                                    type="password")
        if st.form_submit_button("Save"):
            after = {"name": name.strip(), "phone": norm_phone(phone), "role": role, "active": active}
            if not after["name"] or (s is None and not email.strip()):
                st.error("Name and email are required.")
            elif s and s["id"] == user["id"] and (role != "admin" or not active):
                st.error("You can't remove your own admin access.")
            elif s is None and password_problem(init_pw, init_pw):
                st.error(password_problem(init_pw, init_pw))
            else:
                try:
                    with pool().connection() as conn, conn.transaction():
                        if s is None:
                            new_id = conn.execute("""INSERT INTO staff (name, email, phone, role, active,
                                                                        password_hash)
                                                     VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                                  (after["name"], email.strip().lower(), after["phone"],
                                                   role, active, hash_password(init_pw))).fetchone()["id"]
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

    if s is not None:
        with st.expander(f"Reset password for {s['name']}"):
            st.caption("For a forgotten password: set a temporary one, tell them in person or by phone, "
                       "and ask them to change it under My account.")
            with st.form(f"reset_{sid}", clear_on_submit=True):
                temp = st.text_input(f"Temporary password (min {MIN_PW} characters)", type="password")
                temp2 = st.text_input("Repeat it", type="password")
                if st.form_submit_button("Reset password"):
                    problem = password_problem(temp, temp2)
                    if problem:
                        st.error(problem)
                    else:
                        with pool().connection() as conn, conn.transaction():
                            conn.execute("UPDATE staff SET password_hash = %s WHERE id = %s",
                                         (hash_password(temp), int(s["id"])))
                            audit(conn, user["id"], "reset_password", "staff", int(s["id"]))
                        st.success(f"Password reset for {s['name']}.")

# ── Monthly rent ─────────────────────────────────────────────────────────────
with rent_tab:
    st.info("**What is this?** Each month the system writes down the rent every active tenant owes for that "
            "month. Today, the Reminder Queue, the Rent Board and Reports all work from it.\n\n"
            "**It happens by itself:** next month's rent is prepared on the **25th at 8:00am**, and this month's "
            "is checked **every night at 12:30am**. Use the button below only when you've just added a tenancy "
            "and want its rent to show straight away. Pressing it twice never doubles anything.")
    last = query("""SELECT finished_at AT TIME ZONE 'Asia/Kuching' AS at, status, rows_affected, job
                    FROM job_runs WHERE job LIKE 'rent_schedule%%' ORDER BY started_at DESC LIMIT 1""")
    if not last.empty:
        r = last.iloc[0]
        when = f"{r['at']:%d %b %Y, %I:%M %p}" if r["at"] is not None else "still running"
        msg = (f"Last automatic run: {when} · {'OK' if r['status'] == 'ok' else r['status'].upper()} · "
               f"{r['rows_affected'] or 0} added")
        if r["status"] == "ok":
            st.caption(msg)
        else:
            st.warning(msg)

    today = dt.date.today().replace(day=1)
    nxt = (today + dt.timedelta(days=32)).replace(day=1)
    prv = (today - dt.timedelta(days=1)).replace(day=1)
    labels = {today: f"This month ({today:%B %Y})", nxt: f"Next month ({nxt:%B %Y})",
              prv: f"Last month ({prv:%B %Y})"}
    month = st.radio("Month", list(labels), format_func=labels.get, horizontal=True)
    month_end = (month + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    plan = query("""SELECT u.code AS unit, tn.name AS tenant, t.monthly_rent AS rent,
                           GREATEST(make_date(%(y)s, %(m)s, t.due_day), t.start_date) AS due,
                           t.start_date, t.end_date, t.status,
                           rs.id IS NOT NULL AS prepared,
                           (t.status = 'active' AND t.start_date <= %(e)s
                            AND (t.end_date IS NULL OR t.end_date >= %(p)s)) AS covered
                    FROM tenancies t
                    JOIN units u ON u.id = t.unit_id
                    JOIN tenants tn ON tn.id = t.tenant_id
                    LEFT JOIN rent_schedule rs ON rs.tenancy_id = t.id AND rs.period = %(p)s
                    WHERE t.status IN ('active', 'upcoming') OR rs.id IS NOT NULL
                    ORDER BY u.code""", {"p": month, "e": month_end, "y": month.year, "m": month.month})
    if plan.empty:
        st.info("No tenancies yet. Start one in Register.")
    else:
        def state(r):
            if r["prepared"]:
                return "✅ Already prepared"
            if r["covered"]:
                return "➕ Will be added"
            if r["status"] == "upcoming":
                return "⏸ Upcoming tenancy (not active yet)"
            return "⏸ Not this month (outside tenancy dates)"
        plan["state"] = plan.apply(state, axis=1)
        to_add = int((plan["state"] == "➕ Will be added").sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Already prepared", int(plan["prepared"].sum()))
        c2.metric("Will be added", to_add)
        c3.metric("Rent to add", f"RM{plan.loc[plan['state'] == '➕ Will be added', 'rent'].astype(float).sum():,.2f}")
        st.dataframe(plan[["state", "unit", "tenant", "rent", "due", "start_date", "end_date"]],
                     hide_index=True, width="stretch", column_config={
                         "state": "", "unit": "Unit", "tenant": "Tenant",
                         "rent": st.column_config.NumberColumn("Monthly rent", format="RM %.2f"),
                         "due": st.column_config.DateColumn(f"Due in {month:%B}", format="DD/MM/YYYY"),
                         "start_date": st.column_config.DateColumn("Tenancy starts", format="DD/MM/YYYY"),
                         "end_date": st.column_config.DateColumn("Tenancy ends", format="DD/MM/YYYY")})
        if to_add == 0:
            st.success(f"Everything is already prepared for {month:%B %Y}.")
        elif st.button(f"Prepare {month:%B %Y} rent now ({to_add} to add)", type="primary"):
            with pool().connection() as conn, conn.transaction():
                n = conn.execute("SELECT generate_rent_schedule(%s) AS n", (month,)).fetchone()["n"]
                audit(conn, user["id"], "generate_rent_schedule", "rent_schedule", None, None,
                      {"period": month, "created": n})
            st.success(f"Added {month:%B %Y} rent for {n} tenancy(ies). If a tenancy starts or ends mid-month, "
                       "its amount is marked 'check pro-rata': adjust it on the Tenancy page.")
            st.rerun()

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
