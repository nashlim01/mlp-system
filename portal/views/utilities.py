import datetime as dt

import streamlit as st

from db import audit, execute, pool, query

user = st.session_state.user
can_edit = user["role"] != "viewer"
st.title("Utilities")
st.caption("Electricity (Sarawak Energy) and water bills for every unit. Each new bill is added to the tenant's "
           "monthly total: the newest bill issued before that month's due date.")

# ── SEB check: last run and run now ──────────────────────────────────────────
last = execute("""SELECT started_at AT TIME ZONE 'Asia/Kuching' AS started,
                         finished_at AT TIME ZONE 'Asia/Kuching' AS finished,
                         status, rows_affected, left(error, 800) AS error
                  FROM job_runs WHERE job = 'electric_check' ORDER BY started_at DESC LIMIT 1""")
waiting = execute("""SELECT requested_at AT TIME ZONE 'Asia/Kuching' AS at FROM job_requests
                     WHERE job = 'electric_check' AND picked_at IS NULL ORDER BY requested_at DESC LIMIT 1""")
c1, c2 = st.columns([3, 1])
with c1:
    if last is None:
        st.info("⚡ The SEB check hasn't run yet. It runs every morning at 7:00 once the company SEBCares "
                "login is set up.")
    elif last["status"] == "ok":
        st.success(f"⚡ Last SEB check {last['finished']:%d %b %Y, %H:%M}: {last['rows_affected']} account(s) read.")
    elif last["status"] == "running":
        st.info(f"⚡ An SEB check started at {last['started']:%H:%M} is still running.")
    else:
        st.error(f"⚡ Last SEB check ({last['started']:%d %b %Y, %H:%M}) had problems; "
                 f"{last['rows_affected'] or 0} account(s) read.")
        with st.expander("Details"):
            st.code(last["error"])
with c2:
    if waiting:
        st.caption(f"⏳ Check requested at {waiting['at']:%H:%M}; the worker starts it within a minute.")
    elif can_edit and st.button("🔄 Check SEB now", width="stretch"):
        execute("INSERT INTO job_requests (job, requested_by) VALUES ('electric_check', %s) RETURNING id",
                (user["id"],))
        st.rerun()

# ── Every unit ───────────────────────────────────────────────────────────────
units = query("""
    WITH latest AS (
      SELECT DISTINCT ON (b.utility_account_id) b.*, c.rent_schedule_id, rs.period AS charged_to
      FROM utility_bills b
      LEFT JOIN utility_charges c ON c.utility_bill_id = b.id
      LEFT JOIN rent_schedule rs ON rs.id = c.rent_schedule_id
      WHERE NOT b.reversed ORDER BY b.utility_account_id, b.bill_date DESC)
    SELECT u.code, tn.name AS tenant, u.assigned_staff_id,
           e.account_no AS seb_account, eb.amount AS seb_amount, eb.bill_date AS seb_bill_date,
           eb.due_date AS seb_due, eb.charged_to AS seb_month, ck.outstanding AS seb_outstanding,
           w.account_no AS water_account, wb.amount AS water_amount, wb.bill_date AS water_bill_date,
           wb.charged_to AS water_month
    FROM units u
    LEFT JOIN tenancies t ON t.unit_id = u.id AND t.status = 'active'
    LEFT JOIN tenants tn ON tn.id = t.tenant_id
    LEFT JOIN utility_accounts e ON e.unit_id = u.id AND e.type = 'electric'
    LEFT JOIN latest eb ON eb.utility_account_id = e.id
    LEFT JOIN LATERAL (SELECT outstanding FROM bill_checks bc WHERE bc.utility_account_id = e.id
                       ORDER BY checked_at DESC LIMIT 1) ck ON TRUE
    LEFT JOIN utility_accounts w ON w.unit_id = u.id AND w.type = 'water'
    LEFT JOIN latest wb ON wb.utility_account_id = w.id
    WHERE u.status <> 'inactive'
    ORDER BY u.code""")
c1, c2 = st.columns(2)
mine = c1.toggle("Only my units", value=user["role"] != "admin")
missing = c2.toggle("Only units missing an account number", value=False)
view = units
if mine:
    view = view[view["assigned_staff_id"] == user["id"]]
if missing:
    view = view[view["seb_account"].isna() | view["water_account"].isna()]
if not view.empty:
    view = view.copy()
    for col in ("seb_month", "water_month"):
        view[col] = view[col].map(lambda d: f"{d:%b %Y}" if d is not None else "")
    for col in ("seb_amount", "seb_outstanding", "water_amount"):            # blanks, not "None"
        view[col] = view[col].map(lambda v: f"RM {float(v):,.2f}" if v is not None else "")
    for col in ("seb_bill_date", "seb_due", "water_bill_date"):
        view[col] = view[col].map(lambda d: f"{d:%d/%m/%Y}" if d is not None else "")
    for col in ("tenant", "seb_account", "water_account"):
        view[col] = view[col].fillna("")
    view = view.sort_values(["seb_account", "code"], ascending=[False, True])   # units with accounts first
    m1, m2, m3 = st.columns(3)
    m1.metric("Units", len(view))
    m2.metric("Without SEB account", int((view["seb_account"] == "").sum()))
    m3.metric("Without water account", int((view["water_account"] == "").sum()))
    money = day = lambda label: label
    st.dataframe(view.drop(columns="assigned_staff_id"), hide_index=True, width="stretch", column_config={
        "code": "Unit", "tenant": "Tenant", "seb_account": "⚡ SEB account", "seb_amount": money("Latest bill"),
        "seb_bill_date": day("Bill date"), "seb_due": day("SEB due"), "seb_month": "Added to",
        "seb_outstanding": money("Unpaid at SEB"), "water_account": "💧 Water account",
        "water_amount": money("Latest water bill"), "water_bill_date": day("Water bill date"),
        "water_month": "Added to"})
    st.caption("Account numbers are set on the Units page. \"Added to\" = the rent month the bill was charged to.")
else:
    st.info("No units match.")

# ── SEB accounts found on the portal but not linked to a unit ────────────────
loose = query("""SELECT a.id, a.account_no, a.nickname, a.address, a.last_seen_at
                 FROM utility_accounts a WHERE a.type = 'electric' AND a.unit_id IS NULL
                 ORDER BY a.account_no""")
if not loose.empty:
    st.subheader(f"🔗 SEB accounts not linked to a unit ({len(loose)})")
    st.caption("Found on the company SEBCares login. Link each one so its bills reach the right tenant.")
    st.dataframe(loose.drop(columns="id"), hide_index=True, width="stretch", column_config={
        "account_no": "SEB account", "nickname": "Nickname on SEBCares", "address": "Address",
        "last_seen_at": st.column_config.DatetimeColumn("Last seen", format="DD/MM/YYYY")})
    if can_edit:
        free = query("""SELECT u.id, u.code, u.address FROM units u WHERE u.status <> 'inactive'
                        AND NOT EXISTS (SELECT 1 FROM utility_accounts a WHERE a.unit_id = u.id AND a.type = 'electric')
                        ORDER BY u.code""")
        if free.empty:
            st.caption("Every active unit already has an SEB account.")
        else:
            with st.form("link_seb"):
                c1, c2 = st.columns(2)
                acc = c1.selectbox("SEB account", loose["id"].tolist(), format_func=lambda i: (
                    lambda r: f"{r['account_no']} · {r['nickname'] or ''} · {r['address'] or ''}")(
                    loose[loose["id"] == i].iloc[0]))
                unit = c2.selectbox("Unit", free["id"].tolist(), format_func=lambda i: (
                    lambda r: f"{r['code']} · {r['address']}")(free[free["id"] == i].iloc[0]))
                if st.form_submit_button("Link", type="primary"):
                    with pool().connection() as conn, conn.transaction():
                        conn.execute("UPDATE utility_accounts SET unit_id = %s WHERE id = %s AND unit_id IS NULL",
                                     (int(unit), int(acc)))
                        conn.execute("SELECT attach_utility_bills()")
                        audit(conn, user["id"], "link_utility_account", "units", int(unit), None,
                              {"utility_account_id": int(acc)})
                    st.success("Linked."); st.rerun()

# ── Water bills (typed in until the Sarawak water scraper is ready) ──────────
st.subheader("💧 Enter a water bill")
water = query("""SELECT a.id, u.code, a.account_no, tn.name AS tenant FROM utility_accounts a
                 JOIN units u ON u.id = a.unit_id
                 LEFT JOIN tenancies t ON t.unit_id = u.id AND t.status = 'active'
                 LEFT JOIN tenants tn ON tn.id = t.tenant_id
                 WHERE a.type = 'water' ORDER BY u.code""")
if water.empty:
    st.caption("No unit has a water account number yet: add them on the Units page.")
elif can_edit:
    with st.form("water_bill", clear_on_submit=True):
        c1, c2 = st.columns(2)
        acc = c1.selectbox("Unit", water["id"].tolist(), format_func=lambda i: (
            lambda r: f"{r['code']} · {r['account_no']} · {r['tenant'] or 'vacant'}")(water[water["id"] == i].iloc[0]))
        amount = c2.number_input("Bill amount (RM)", min_value=0.0, step=1.0)
        bill_date = c1.date_input("Bill date", value=dt.date.today(), format="DD/MM/YYYY")
        due = c2.date_input("Due date (optional)", value=None, format="DD/MM/YYYY")
        bill_no = c1.text_input("Bill no. (optional)")
        if st.form_submit_button("Save water bill", type="primary"):
            if amount <= 0:
                st.error("Enter the bill amount.")
            else:
                no = bill_no.strip() or f"manual-{bill_date:%Y-%m-%d}"
                with pool().connection() as conn, conn.transaction():
                    row = conn.execute("""INSERT INTO utility_bills (utility_account_id, bill_no, bill_date, due_date,
                                                                    amount, source, entered_by)
                                          VALUES (%s, %s, %s, %s, %s, 'manual', %s)
                                          ON CONFLICT (utility_account_id, bill_no) DO NOTHING RETURNING id""",
                                       (int(acc), no, bill_date, due, amount, user["id"])).fetchone()
                    if row:
                        n = conn.execute("SELECT attach_utility_bills() AS n").fetchone()["n"]
                        audit(conn, user["id"], "add_water_bill", "utility_bills", row["id"], None,
                              {"amount": amount, "bill_date": str(bill_date)})
                if not row:
                    st.error("That bill is already saved.")
                else:
                    st.success("Saved" + (" and added to the tenant's monthly total." if n else
                                          ". It will be added once the matching rent month exists."))
recent = query("""SELECT u.code, b.bill_date, b.amount, b.bill_no, rs.period AS added_to, s.name AS entered_by
                  FROM utility_bills b JOIN utility_accounts a ON a.id = b.utility_account_id
                  JOIN units u ON u.id = a.unit_id
                  LEFT JOIN utility_charges c ON c.utility_bill_id = b.id
                  LEFT JOIN rent_schedule rs ON rs.id = c.rent_schedule_id
                  LEFT JOIN staff s ON s.id = b.entered_by
                  WHERE a.type = 'water' ORDER BY b.first_seen_at DESC LIMIT 20""")
if not recent.empty:
    with st.expander("Recent water bills"):
        recent["added_to"] = recent["added_to"].map(lambda d: f"{d:%b %Y}" if d is not None else "not yet")
        st.dataframe(recent, hide_index=True, width="stretch")
