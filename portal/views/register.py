import datetime as dt

import streamlit as st

from db import pool, query
from messages import norm_phone
from records import opt, save, sync_unit_status

user = st.session_state.user
can_edit = user["role"] != "viewer"
st.title("Register")
st.caption("Tenancy registration: start, change and end tenancies. Units are on the Units page; landlords and "
           "tenants under Personnel.")

TENANCY_STATUSES = ["active", "upcoming", "ended"]
NEW_TENANT = "➕ New tenant (type details below)"

# ── Tenancies ────────────────────────────────────────────────────────────────
tcs = query("""SELECT t.*, u.code AS unit_code, tn.name AS tenant_name
               FROM tenancies t JOIN units u ON u.id = t.unit_id
               JOIN tenants tn ON tn.id = t.tenant_id
               ORDER BY u.code, t.start_date DESC""")
which = st.multiselect("Show", TENANCY_STATUSES, default=["active", "upcoming"])
if not tcs.empty:
    st.dataframe(tcs[tcs["status"].isin(which)][
                     ["unit_code", "tenant_name", "start_date", "end_date", "monthly_rent",
                      "due_day", "grace_days", "deposit_rental", "deposit_utility", "status"]],
                 hide_index=True, width="stretch", column_config={
                     "unit_code": "Unit", "tenant_name": "Tenant",
                     "start_date": st.column_config.DateColumn("Start", format="DD/MM/YYYY"),
                     "end_date": st.column_config.DateColumn("End", format="DD/MM/YYYY"),
                     "monthly_rent": st.column_config.NumberColumn("Rent", format="RM %.2f"),
                     "due_day": "Due day", "grace_days": "Grace days",
                     "deposit_rental": st.column_config.NumberColumn("Deposit (rental)", format="RM %.2f"),
                     "deposit_utility": st.column_config.NumberColumn("Deposit (utility)", format="RM %.2f"),
                     "status": "Status"})
if can_edit:
    start_tab, edit_tab, end_tab = st.tabs(["➕ Start tenancy", "✏️ Edit tenancy", "🏁 End tenancy"])

    with start_tab:
        all_units = query("""SELECT id, code, status FROM units WHERE status <> 'inactive'
                             ORDER BY status = 'vacant' DESC, code""")
        all_tenants = query("SELECT id, name, phone FROM tenants ORDER BY name")
        if all_units.empty:
            st.info("Add the unit first (Units page).")
        else:
            u_names = dict(zip(all_units["id"], all_units["code"] + " · " + all_units["status"]))
            t_names = {NEW_TENANT: NEW_TENANT, **dict(zip(all_tenants["id"],
                                                          all_tenants["name"] + " · " + all_tenants["phone"].fillna("")))}
            with st.form("start_tenancy", clear_on_submit=True):
                c1, c2 = st.columns(2)
                uid = c1.selectbox("Unit (vacant first)", list(u_names), format_func=u_names.get)
                tid = c2.selectbox("Tenant", list(t_names), format_func=t_names.get)
                st.caption("New tenant? Leave 'New tenant' selected and fill in:")
                n1, n2 = st.columns(2)
                new_name = n1.text_input("New tenant's full name", help="Exactly as on their IC: bank transfers are "
                                                                         "matched on it")
                new_phone = n2.text_input("New tenant's phone", placeholder="012-345 6789")
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=dt.date.today(), format="DD/MM/YYYY")
                end = c2.date_input("End date (optional)", value=None, format="DD/MM/YYYY")
                rent = c1.number_input("Monthly rent (RM)", min_value=0.0, step=50.0)
                due = c2.number_input("Due day (1–28)", min_value=1, max_value=28, value=1,
                                      help="The tenancy cycle: the 1st reminder goes out on this day")
                dep_r = c1.number_input("Deposit, rental (RM)", min_value=0.0, step=50.0)
                dep_u = c2.number_input("Deposit, utility (RM)", min_value=0.0, step=50.0)
                grace = c1.number_input("Grace days after the due date", min_value=0, max_value=60, value=7)
                status = c2.selectbox("Status", ["active", "upcoming"],
                                      help="Use 'upcoming' if the current tenant hasn't moved out yet.")
                notes = st.text_area("Notes")
                if st.form_submit_button("Start tenancy", type="primary"):
                    phone = norm_phone(new_phone)
                    if rent <= 0:
                        st.error("Monthly rent must be more than 0.")
                    elif end and end < start:
                        st.error("End date is before the start date.")
                    elif tid == NEW_TENANT and (not new_name.strip() or not phone):
                        st.error("Type the new tenant's name and phone, or pick an existing tenant.")
                    elif tid == NEW_TENANT and (not phone.startswith("60") or not 10 <= len(phone) <= 13):
                        st.error(f"'{new_phone}' does not look like a Malaysian mobile number.")
                    else:
                        if tid == NEW_TENANT:
                            tid = save("add_tenant", "tenants", """
                                INSERT INTO tenants (name, phone) VALUES (%(name)s, %(phone)s) RETURNING id""",
                                {"name": new_name.strip(), "phone": phone}, None,
                                {"name": new_name.strip(), "phone": phone})
                        after = {"unit_id": int(uid), "tenant_id": tid, "start_date": start,
                                 "end_date": end, "monthly_rent": rent, "due_day": int(due), "grace_days": int(grace),
                                 "deposit_rental": dep_r, "deposit_utility": dep_u, "status": status,
                                 "notes": opt(notes)}
                        if tid and save("start_tenancy", "tenancies", """
                                INSERT INTO tenancies (unit_id, tenant_id, start_date, end_date, monthly_rent,
                                    due_day, grace_days, deposit_rental, deposit_utility, status, notes)
                                VALUES (%(unit_id)s, %(tenant_id)s, %(start_date)s, %(end_date)s, %(monthly_rent)s,
                                    %(due_day)s, %(grace_days)s, %(deposit_rental)s, %(deposit_utility)s,
                                    %(status)s, %(notes)s) RETURNING id""", {**after, "tenant_id": int(tid)},
                                None, after):
                            with pool().connection() as conn:
                                sync_unit_status(conn, int(uid))
                            st.success(f"Tenancy started for {u_names[uid].split(' · ')[0]}. Its monthly rent "
                                       "appears automatically by tomorrow morning (an admin can add it now under "
                                       "Admin → Monthly rent).")

    live = tcs[tcs["status"].isin(["active", "upcoming"])] if not tcs.empty else tcs
    fmt = lambda r: f"{r['unit_code']} · {r['tenant_name']} · {r['status']} from {r['start_date']:%d/%m/%Y}"

    with edit_tab:
        if live.empty:
            st.info("No active or upcoming tenancies.")
        else:
            k = st.selectbox("Tenancy", range(len(live)), key="edit_pick",
                             format_func=lambda k: fmt(live.iloc[k]))
            t = live.iloc[k].to_dict()
            with st.form(f"edit_tenancy_{t['id']}"):
                c1, c2 = st.columns(2)
                rent = c1.number_input("Monthly rent (RM)", min_value=0.01,
                                       value=float(t["monthly_rent"]), step=50.0)
                due = c2.number_input("Due day (1–28)", min_value=1, max_value=28, value=int(t["due_day"]))
                end = c1.date_input("End date", value=t["end_date"], format="DD/MM/YYYY")
                status = c2.selectbox("Status", ["active", "upcoming"],
                                      index=["active", "upcoming"].index(t["status"]))
                dep_r = c1.number_input("Deposit, rental (RM)", min_value=0.0,
                                        value=float(t["deposit_rental"] or 0), step=50.0)
                dep_u = c2.number_input("Deposit, utility (RM)", min_value=0.0,
                                        value=float(t["deposit_utility"] or 0), step=50.0)
                grace = c1.number_input("Grace days after the due date", min_value=0, max_value=60,
                                        value=int(t.get("grace_days", 7) or 0),
                                        help="Overdue only after these days. Reminders: due date, day 5 of 7, last day")
                notes = st.text_area("Notes", value=t["notes"] or "")
                st.caption("A new rent applies to months not prepared yet. To change a month that's "
                           "already prepared, use the Tenancy page.")
                if st.form_submit_button("Save changes"):
                    keys = ["monthly_rent", "due_day", "grace_days", "end_date", "status", "deposit_rental",
                            "deposit_utility", "notes"]
                    after = {"monthly_rent": rent, "due_day": int(due), "grace_days": int(grace), "end_date": end,
                             "status": status, "deposit_rental": dep_r, "deposit_utility": dep_u,
                             "notes": opt(notes)}
                    before = {key: t[key] for key in keys}
                    if end and end < t["start_date"]:
                        st.error("End date is before the start date.")
                    elif save("edit_tenancy", "tenancies", """
                            UPDATE tenancies SET monthly_rent = %(monthly_rent)s, due_day = %(due_day)s,
                                grace_days = %(grace_days)s, end_date = %(end_date)s, status = %(status)s,
                                deposit_rental = %(deposit_rental)s,
                                deposit_utility = %(deposit_utility)s, notes = %(notes)s
                            WHERE id = %(id)s AND status IN ('active', 'upcoming') RETURNING id""",
                            {**after, "id": int(t["id"])}, before, after):
                        with pool().connection() as conn:
                            sync_unit_status(conn, int(t["unit_id"]))
                        st.success("Tenancy saved."); st.rerun()

    with end_tab:
        active = live[live["status"] == "active"] if not live.empty else live
        if active.empty:
            st.info("No active tenancies.")
        else:
            k = st.selectbox("Tenancy", range(len(active)), key="end_pick",
                             format_func=lambda k: fmt(active.iloc[k]))
            t = active.iloc[k].to_dict()
            with st.form(f"end_tenancy_{t['id']}"):
                end = st.date_input("Last day of tenancy", value=t["end_date"] or dt.date.today(),
                                    format="DD/MM/YYYY")
                reason = st.text_input("Reason / note", placeholder="Moved out, deposit refunded")
                st.caption("Months already prepared stay. If the last month should be pro-rata, "
                           "adjust it on the Tenancy page.")
                if st.form_submit_button("End tenancy", type="primary"):
                    if end < t["start_date"]:
                        st.error("End date is before the start date.")
                    elif save("end_tenancy", "tenancies", """
                            UPDATE tenancies SET status = 'ended', end_date = %s,
                                notes = concat_ws(' · ', notes, %s)
                            WHERE id = %s AND status = 'active' RETURNING id""",
                            (end, opt(reason), int(t["id"])),
                            {"status": "active", "end_date": t["end_date"]},
                            {"status": "ended", "end_date": end, "reason": opt(reason)}):
                        with pool().connection() as conn:
                            sync_unit_status(conn, int(t["unit_id"]))
                        st.success("Tenancy ended."); st.rerun()
