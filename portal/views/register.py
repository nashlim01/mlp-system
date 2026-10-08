import datetime as dt

import psycopg
import streamlit as st

from db import audit, pool, query
from messages import norm_phone

user = st.session_state.user
can_edit = user["role"] != "viewer"
st.title("Register")

NEW = "➕ New"
UNIT_STATUSES = ["occupied", "vacant", "inactive"]
TENANCY_STATUSES = ["active", "upcoming", "ended"]


def opt(v):
    """Empty text input -> NULL."""
    v = (v or "").strip()
    return v or None


def save(action, entity, sql, params, before=None, after=None):
    """Run one write + its audit row in a transaction. Returns the row id, or None on error."""
    try:
        with pool().connection() as conn, conn.transaction():
            row = conn.execute(sql, params).fetchone()
            if row is None:
                st.warning("Nothing was saved: the record changed or no longer exists. Reload and retry.")
                return None
            audit(conn, user["id"], action, entity, row["id"], before, after)
            return row["id"]
    except psycopg.errors.UniqueViolation as e:
        msg = str(e)
        if "one_active_tenancy_per_unit" in msg:
            st.error("This unit already has an active tenancy. End it first, or save this one as 'upcoming'.")
        elif "tenants_phone_key" in msg:
            st.error("Another tenant already has this phone number. Search for them in the Tenants tab.")
        else:
            st.error(f"Already exists: {msg.splitlines()[0]}")
    except psycopg.errors.CheckViolation as e:
        st.error(f"Invalid value: {str(e).splitlines()[0]}")
    return None


def sync_unit_status(conn, unit_id):
    conn.execute("""UPDATE units u SET status = CASE
                      WHEN EXISTS (SELECT 1 FROM tenancies t
                                   WHERE t.unit_id = u.id AND t.status = 'active') THEN 'occupied'
                      ELSE 'vacant' END
                    WHERE u.id = %s AND u.status <> 'inactive'""", (unit_id,))


def picker(label, df, fmt, key):
    """Select box over a DataFrame with a 'New' option. Returns the chosen row (dict) or None."""
    opts = [NEW] + list(range(len(df))) if can_edit else list(range(len(df)))
    if not opts:
        return None
    k = st.selectbox(label, opts, key=key,
                     format_func=lambda k: NEW if k == NEW else fmt(df.iloc[k]))
    return None if k == NEW else df.iloc[k].to_dict()


landlords = query("SELECT id, code, name FROM landlords ORDER BY code")
staff = query("SELECT id, name, email FROM staff WHERE active ORDER BY name")

units_tab, tenants_tab, tenancies_tab = st.tabs(["Units", "Tenants", "Tenancies"])
st.caption("Landlords have their own page in the menu.")

# ── Units ────────────────────────────────────────────────────────────────────
with units_tab:
    units = query("""SELECT u.id, u.code, u.address, u.area, u.unit_type, u.status, u.notes,
                            u.landlord_id, u.assigned_staff_id,
                            l.code AS landlord, COALESCE(s.name, 'Unassigned') AS staff
                     FROM units u LEFT JOIN landlords l ON l.id = u.landlord_id
                     LEFT JOIN staff s ON s.id = u.assigned_staff_id ORDER BY u.code""")
    if not units.empty:
        st.dataframe(units[["code", "address", "area", "unit_type", "landlord", "staff", "status"]],
                     hide_index=True, width="stretch")
    if landlords.empty or staff.empty:
        st.info("Add landlords and staff (import the register) before adding units.")
    elif can_edit:
        st.markdown("##### Add or edit a unit")
        u = picker("Unit", units, lambda r: f"{r['code']} · {r['address']}", "unit_pick")
        ll_ids, st_ids = landlords["id"].tolist(), staff["id"].tolist()
        ll_names = dict(zip(landlords["id"], landlords["code"] + " · " + landlords["name"]))
        st_names = dict(zip(staff["id"], staff["name"]))
        with st.form(f"unit_{u['id'] if u else 'new'}"):
            code = st.text_input("Unit code (e.g. SNDN-3418)", value=u["code"] if u else "",
                                 disabled=u is not None,
                                 help="Codes are permanent: they are used in bank matching.")
            address = st.text_input("Address *", value=(u or {}).get("address") or "")
            c1, c2 = st.columns(2)
            area = c1.text_input("Area", value=(u or {}).get("area") or "")
            unit_type = c2.text_input("Unit type", value=(u or {}).get("unit_type") or "")
            ll = c1.selectbox("Landlord *", ll_ids, format_func=ll_names.get,
                              index=ll_ids.index(u["landlord_id"]) if u and u["landlord_id"] in ll_ids else 0)
            sid = c2.selectbox("Assigned staff *", st_ids, format_func=st_names.get,
                               index=st_ids.index(u["assigned_staff_id"]) if u and u["assigned_staff_id"] in st_ids else 0)
            status = st.selectbox("Status", UNIT_STATUSES,
                                  index=UNIT_STATUSES.index(u["status"]) if u else 1,
                                  help="occupied/vacant follow the tenancies automatically; use inactive to retire a unit.")
            notes = st.text_area("Notes", value=(u or {}).get("notes") or "")
            if st.form_submit_button("Save unit"):
                after = {"code": code.strip().upper(), "address": address.strip(), "area": opt(area),
                         "unit_type": opt(unit_type), "landlord_id": int(ll),
                         "assigned_staff_id": int(sid), "status": status, "notes": opt(notes)}
                if not after["code"] or not after["address"]:
                    st.error("Unit code and address are required.")
                elif u is None:
                    if save("add_unit", "units", """
                            INSERT INTO units (code, address, area, unit_type, landlord_id,
                                               assigned_staff_id, status, notes)
                            VALUES (%(code)s, %(address)s, %(area)s, %(unit_type)s, %(landlord_id)s,
                                    %(assigned_staff_id)s, %(status)s, %(notes)s) RETURNING id""",
                            after, None, after):
                        st.success(f"Added {after['code']}."); st.rerun()
                else:
                    before = {k: u[k] for k in after if k != "code"}
                    after.pop("code")
                    if save("edit_unit", "units", """
                            UPDATE units SET address = %(address)s, area = %(area)s,
                                unit_type = %(unit_type)s, landlord_id = %(landlord_id)s,
                                assigned_staff_id = %(assigned_staff_id)s, status = %(status)s,
                                notes = %(notes)s
                            WHERE id = %(id)s RETURNING id""",
                            {**after, "id": int(u["id"])}, before, after):
                        st.success("Unit saved."); st.rerun()

# ── Tenants ──────────────────────────────────────────────────────────────────
with tenants_tab:
    tenants = query("SELECT id, name, phone, email, notes FROM tenants ORDER BY name")
    search = st.text_input("Search tenants", placeholder="Name or phone")
    if not tenants.empty:
        shown = tenants
        if search.strip():
            s = search.strip().lower()
            digits = norm_phone(s) or "~"
            shown = tenants[tenants["name"].str.lower().str.contains(s, regex=False)
                            | tenants["phone"].fillna("").str.contains(digits, regex=False)]
        st.dataframe(shown[["name", "phone", "email", "notes"]], hide_index=True, width="stretch")
    if can_edit:
        st.markdown("##### Add or edit a tenant")
        tn = picker("Tenant", tenants, lambda r: f"{r['name']} · {r['phone'] or 'no phone'}", "tenant_pick")
        with st.form(f"tenant_{tn['id'] if tn else 'new'}"):
            name = st.text_input("Name *", value=(tn or {}).get("name") or "")
            phone = st.text_input("Phone *", value=(tn or {}).get("phone") or "",
                                  help="Any format, e.g. 012-345 6789. Saved as 60123456789 for WhatsApp.")
            email = st.text_input("Email", value=(tn or {}).get("email") or "")
            notes = st.text_area("Notes (no IC numbers)", value=(tn or {}).get("notes") or "")
            if st.form_submit_button("Save tenant"):
                after = {"name": name.strip(), "phone": norm_phone(phone), "email": opt(email),
                         "notes": opt(notes)}
                if not after["name"] or not after["phone"]:
                    st.error("Name and phone are required.")
                elif not after["phone"].startswith("60") or not 10 <= len(after["phone"]) <= 13:
                    st.error(f"'{phone}' does not look like a Malaysian mobile number.")
                elif tn is None:
                    if save("add_tenant", "tenants", """
                            INSERT INTO tenants (name, phone, email, notes)
                            VALUES (%(name)s, %(phone)s, %(email)s, %(notes)s) RETURNING id""",
                            after, None, after):
                        st.success(f"Added {after['name']}."); st.rerun()
                else:
                    before = {k: tn[k] for k in after}
                    if save("edit_tenant", "tenants", """
                            UPDATE tenants SET name = %(name)s, phone = %(phone)s, email = %(email)s,
                                notes = %(notes)s
                            WHERE id = %(id)s RETURNING id""",
                            {**after, "id": int(tn["id"])}, before, after):
                        st.success("Tenant saved."); st.rerun()

# ── Tenancies ────────────────────────────────────────────────────────────────
with tenancies_tab:
    tcs = query("""SELECT t.*, u.code AS unit_code, tn.name AS tenant_name
                   FROM tenancies t JOIN units u ON u.id = t.unit_id
                   JOIN tenants tn ON tn.id = t.tenant_id
                   ORDER BY u.code, t.start_date DESC""")
    which = st.multiselect("Show", TENANCY_STATUSES, default=["active", "upcoming"])
    if not tcs.empty:
        st.dataframe(tcs[tcs["status"].isin(which)][
                         ["unit_code", "tenant_name", "start_date", "end_date", "monthly_rent",
                          "due_day", "deposit_rental", "deposit_utility", "status"]],
                     hide_index=True, width="stretch")
    if can_edit:

        start_tab, edit_tab, end_tab = st.tabs(["Start tenancy", "Edit tenancy", "End tenancy"])

        with start_tab:
            all_units = query("SELECT id, code FROM units WHERE status <> 'inactive' ORDER BY code")
            all_tenants = query("SELECT id, name, phone FROM tenants ORDER BY name")
            if all_units.empty or all_tenants.empty:
                st.info("Add the unit and the tenant first.")
            else:
                u_names = dict(zip(all_units["id"], all_units["code"]))
                t_names = dict(zip(all_tenants["id"], all_tenants["name"] + " · " + all_tenants["phone"].fillna("")))
                with st.form("start_tenancy", clear_on_submit=True):
                    c1, c2 = st.columns(2)
                    uid = c1.selectbox("Unit", list(u_names), format_func=u_names.get)
                    tid = c2.selectbox("Tenant", list(t_names), format_func=t_names.get)
                    start = c1.date_input("Start date", value=dt.date.today(), format="DD/MM/YYYY")
                    end = c2.date_input("End date (optional)", value=None, format="DD/MM/YYYY")
                    rent = c1.number_input("Monthly rent (RM)", min_value=0.0, step=50.0)
                    due = c2.number_input("Due day (1–28)", min_value=1, max_value=28, value=1)
                    dep_r = c1.number_input("Deposit, rental (RM)", min_value=0.0, step=50.0)
                    dep_u = c2.number_input("Deposit, utility (RM)", min_value=0.0, step=50.0)
                    status = st.selectbox("Status", ["active", "upcoming"],
                                          help="Use 'upcoming' if the current tenant hasn't moved out yet.")
                    notes = st.text_area("Notes")
                    if st.form_submit_button("Start tenancy"):
                        after = {"unit_id": int(uid), "tenant_id": int(tid), "start_date": start,
                                 "end_date": end, "monthly_rent": rent, "due_day": int(due),
                                 "deposit_rental": dep_r, "deposit_utility": dep_u, "status": status,
                                 "notes": opt(notes)}
                        if rent <= 0:
                            st.error("Monthly rent must be more than 0.")
                        elif end and end < start:
                            st.error("End date is before the start date.")
                        elif save("start_tenancy", "tenancies", """
                                INSERT INTO tenancies (unit_id, tenant_id, start_date, end_date,
                                    monthly_rent, due_day, deposit_rental, deposit_utility, status, notes)
                                VALUES (%(unit_id)s, %(tenant_id)s, %(start_date)s, %(end_date)s,
                                    %(monthly_rent)s, %(due_day)s, %(deposit_rental)s,
                                    %(deposit_utility)s, %(status)s, %(notes)s) RETURNING id""",
                                after, None, after):
                            with pool().connection() as conn:
                                sync_unit_status(conn, int(uid))
                            st.success(f"Tenancy started for {u_names[uid]}. Its monthly rent appears "
                                       "automatically by tomorrow morning (an admin can add it now under "
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
                    notes = st.text_area("Notes", value=t["notes"] or "")
                    st.caption("A new rent applies to months not prepared yet. To change a month that's "
                               "already prepared, use the Tenancy page.")
                    if st.form_submit_button("Save changes"):
                        keys = ["monthly_rent", "due_day", "end_date", "status", "deposit_rental",
                                "deposit_utility", "notes"]
                        after = {"monthly_rent": rent, "due_day": int(due), "end_date": end,
                                 "status": status, "deposit_rental": dep_r, "deposit_utility": dep_u,
                                 "notes": opt(notes)}
                        before = {key: t[key] for key in keys}
                        if end and end < t["start_date"]:
                            st.error("End date is before the start date.")
                        elif save("edit_tenancy", "tenancies", """
                                UPDATE tenancies SET monthly_rent = %(monthly_rent)s, due_day = %(due_day)s,
                                    end_date = %(end_date)s, status = %(status)s,
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
