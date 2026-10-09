import streamlit as st

from db import audit, pool, query
from records import delete_button, opt, picker, save

user = st.session_state.user
can_edit = user["role"] != "viewer"
is_admin = user["role"] == "admin"
st.title("Units")
st.caption("Every unit we manage. Occupied / vacant follows the tenancies (Register); use inactive to retire a unit.")

UNIT_STATUSES = ["occupied", "vacant", "inactive"]
units = query("""
    SELECT u.id, u.code, u.address, u.area, u.unit_type, u.notes, u.landlord_id, u.assigned_staff_id,
           CASE WHEN u.status = 'inactive' THEN 'inactive' WHEN t.id IS NOT NULL THEN 'occupied'
                ELSE 'vacant' END AS status,                         -- follows the tenancies
           COALESCE(l.code || ' · ' || l.name, '—') AS landlord, COALESCE(s.name, 'Unassigned') AS staff,
           tn.name AS tenant, t.monthly_rent, t.end_date,
           (SELECT string_agg(account_no, ', ') FROM utility_accounts a WHERE a.unit_id = u.id AND a.type = 'electric')
             AS seb_account,
           (SELECT string_agg(account_no, ', ') FROM utility_accounts a WHERE a.unit_id = u.id AND a.type = 'water')
             AS water_account,
           (SELECT count(*) FROM tenancies x WHERE x.unit_id = u.id) AS tenancies
    FROM units u
    LEFT JOIN landlords l ON l.id = u.landlord_id
    LEFT JOIN staff s ON s.id = u.assigned_staff_id
    LEFT JOIN tenancies t ON t.unit_id = u.id AND t.status = 'active'
    LEFT JOIN tenants tn ON tn.id = t.tenant_id
    ORDER BY u.code""")

search = st.text_input("Search units", placeholder="Unit code, address, area, tenant or account number")
shown = units
if search.strip() and not units.empty:
    s = search.strip().lower()
    hay = (units["code"] + " " + units["address"].fillna("") + " " + units["area"].fillna("") + " "
           + units["tenant"].fillna("") + " " + units["seb_account"].fillna("") + " "
           + units["water_account"].fillna("")).str.lower()
    shown = units[hay.str.contains(s, regex=False)]

COLS = {"code": "Unit", "address": "Address", "area": "Area", "unit_type": "Type", "landlord": "Landlord",
        "staff": "Staff", "tenant": "Tenant", "monthly_rent": "Rent", "end_date": "Tenancy ends",
        "seb_account": "SEB account", "water_account": "Water account"}
counts = shown["status"].value_counts() if not shown.empty else {}
tabs = st.tabs([f"🟢 Occupied ({counts.get('occupied', 0)})", f"⚪ Vacant ({counts.get('vacant', 0)})",
                f"⛔ Inactive ({counts.get('inactive', 0)})"])
for tab, status in zip(tabs, UNIT_STATUSES):
    with tab:
        part = shown[shown["status"] == status] if not shown.empty else shown
        cols = list(COLS) if status == "occupied" else [c for c in COLS if c not in ("tenant", "monthly_rent",
                                                                                      "end_date")]
        if part.empty:
            st.caption("None.")
        else:
            part = part[cols].copy()
            for c in part.columns:                                   # blanks, not "None"
                if c == "monthly_rent":
                    part[c] = part[c].map(lambda v: f"RM {float(v):,.2f}" if v is not None else "")
                elif c == "end_date":
                    part[c] = part[c].map(lambda d: f"{d:%d/%m/%Y}" if d is not None else "")
                else:
                    part[c] = part[c].fillna("")
            st.dataframe(part, hide_index=True, width="stretch", column_config=COLS)

st.divider()
landlords = query("SELECT id, code, name FROM landlords ORDER BY code")
staff = query("SELECT id, name FROM staff WHERE active ORDER BY name")
u = picker("Unit", shown.reset_index(drop=True), lambda r: f"{r['code']} · {r['address']}", "unit_pick", can_edit)

if can_edit:
    if landlords.empty or staff.empty:
        st.info("Add a landlord (Personnel) and staff (Admin) before adding units."); st.stop()
    st.markdown("##### " + ("Add a unit" if u is None else f"Edit {u['code']}"))
    ll_ids, st_ids = landlords["id"].tolist(), staff["id"].tolist()
    ll_names = dict(zip(landlords["id"], landlords["code"] + " · " + landlords["name"]))
    st_names = dict(zip(staff["id"], staff["name"]))
    with st.form(f"unit_{u['id'] if u else 'new'}"):
        code = st.text_input("Unit code (e.g. SNDN-3418)", value=u["code"] if u else "", disabled=u is not None,
                             help="Codes are permanent: they are used in bank matching.")
        address = st.text_input("Address *", value=(u or {}).get("address") or "")
        c1, c2 = st.columns(2)
        area = c1.text_input("Area", value=(u or {}).get("area") or "")
        unit_type = c2.text_input("Unit type", value=(u or {}).get("unit_type") or "")
        ll = c1.selectbox("Landlord *", ll_ids, format_func=ll_names.get,
                          index=ll_ids.index(u["landlord_id"]) if u and u["landlord_id"] in ll_ids else 0)
        sid = c2.selectbox("Assigned staff *", st_ids, format_func=st_names.get,
                           index=st_ids.index(u["assigned_staff_id"]) if u and u["assigned_staff_id"] in st_ids else 0)
        seb = c1.text_input("SEB account no.", value=(u or {}).get("seb_account") or "",
                            help="Sarawak Energy contract account, 12 digits (e.g. 100003716138). "
                                 "Its bills are added to the tenant's monthly total.")
        water = c2.text_input("Water account no.", value=(u or {}).get("water_account") or "")
        status = st.selectbox("Status", UNIT_STATUSES, index=UNIT_STATUSES.index(u["status"]) if u else 1,
                              help="occupied/vacant follow the tenancies automatically; use inactive to retire a unit.")
        notes = st.text_area("Notes", value=(u or {}).get("notes") or "")
        if st.form_submit_button("Save unit", type="primary"):
            after = {"code": code.strip().upper(), "address": address.strip(), "area": opt(area),
                     "unit_type": opt(unit_type), "landlord_id": int(ll), "assigned_staff_id": int(sid),
                     "status": status, "notes": opt(notes)}
            uid = None
            if not after["code"] or not after["address"]:
                st.error("Unit code and address are required.")
            elif u is None:
                uid = save("add_unit", "units", """
                    INSERT INTO units (code, address, area, unit_type, landlord_id, assigned_staff_id, status, notes)
                    VALUES (%(code)s, %(address)s, %(area)s, %(unit_type)s, %(landlord_id)s, %(assigned_staff_id)s,
                            %(status)s, %(notes)s) RETURNING id""", after, None, after)
            else:
                after.pop("code")
                uid = save("edit_unit", "units", """
                    UPDATE units SET address = %(address)s, area = %(area)s, unit_type = %(unit_type)s,
                        landlord_id = %(landlord_id)s, assigned_staff_id = %(assigned_staff_id)s,
                        status = %(status)s, notes = %(notes)s
                    WHERE id = %(id)s RETURNING id""", {**after, "id": int(u["id"])},
                    {k: u[k] for k in after}, after)
            if uid:
                linked_ok = True
                # utility accounts: link the typed number to this unit, unlink any other of that type
                for kind, typed in (("electric", seb), ("water", water)):
                    acct = "".join(typed.split())
                    old = (u or {}).get("seb_account" if kind == "electric" else "water_account")
                    if (acct or None) == (old or None):
                        continue
                    with pool().connection() as conn, conn.transaction():
                        taken = conn.execute("""SELECT u.code FROM utility_accounts a JOIN units u ON u.id = a.unit_id
                                                WHERE a.type = %s AND a.account_no = %s AND a.unit_id <> %s""",
                                             (kind, acct, uid)).fetchone() if acct else None
                        if taken:
                            st.error(f"{kind.title()} account {acct} is linked to {taken['code']}: unlink it there first.")
                            linked_ok = False
                            continue
                        conn.execute("UPDATE utility_accounts SET unit_id = NULL WHERE unit_id = %s AND type = %s",
                                     (uid, kind))
                        if acct:
                            conn.execute("""INSERT INTO utility_accounts (unit_id, type, account_no) VALUES (%s, %s, %s)
                                            ON CONFLICT (type, account_no) DO UPDATE SET unit_id = EXCLUDED.unit_id""",
                                         (uid, kind, acct))
                        audit(conn, user["id"], "link_utility_account", "units", uid,
                              {"type": kind, "account_no": old}, {"type": kind, "account_no": acct or None})
                if linked_ok:
                    st.success(f"Saved {u['code'] if u else after['code']}."); st.rerun()

if u and is_admin:
    if int(u["tenancies"]):
        st.caption("Units with tenancies can't be deleted (history is kept): set the status to inactive instead.")
    else:
        delete_button(f"unit {u['code']}", f"del_unit_{u['id']}", """
            WITH a AS (UPDATE utility_accounts SET unit_id = NULL WHERE unit_id = %(id)s)
            DELETE FROM units WHERE id = %(id)s RETURNING id""", {"id": int(u["id"])},
            "delete_unit", "units", {"code": u["code"], "address": u["address"]})
