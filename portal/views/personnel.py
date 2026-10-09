import psycopg
import streamlit as st

from db import audit, pool, query
from messages import norm_phone
from records import delete_button, opt, picker, save

user = st.session_state.user
can_edit = user["role"] != "viewer"
is_admin = user["role"] == "admin"
st.title("Personnel")
st.caption("Everyone we deal with: landlords who own the units, and tenants who rent them.")

landlord_tab, tenant_tab = st.tabs(["🏠 Landlords", "👤 Tenants"])

# ── Landlords ────────────────────────────────────────────────────────────────
with landlord_tab:
    lls = query("""SELECT l.id, l.code, l.name, l.phone, l.email, l.management_fee_pct, l.notes,
                          count(u.id) AS units,
                          string_agg(u.code, ', ' ORDER BY u.code) AS unit_codes,
                          (b.landlord_id IS NOT NULL AND b.account_no IS NOT NULL) AS bank_on_file
                   FROM landlords l
                   LEFT JOIN units u ON u.landlord_id = l.id
                   LEFT JOIN landlord_bank b ON b.landlord_id = l.id
                   GROUP BY l.id, b.landlord_id, b.account_no ORDER BY l.code""")
    search = st.text_input("Search landlords", placeholder="Name, code or unit (e.g. Ngui, L001, 2273)")
    shown = lls
    if search.strip() and not lls.empty:
        s = search.strip().lower()
        shown = lls[lls["name"].str.lower().str.contains(s, regex=False)
                    | lls["code"].str.lower().str.contains(s, regex=False)
                    | lls["unit_codes"].fillna("").str.lower().str.contains(s, regex=False)]
    st.caption(f"{len(shown)} of {len(lls)} landlords")
    if not shown.empty:
        st.dataframe(shown[["code", "name", "phone", "email", "management_fee_pct", "units", "unit_codes",
                            "bank_on_file"]].fillna({"phone": "", "email": "", "unit_codes": ""}),
                     hide_index=True, width="stretch", column_config={
                         "code": "Code", "name": "Name", "phone": "Phone", "email": "Email",
                         "management_fee_pct": st.column_config.NumberColumn("Fee %", format="%.1f%%"),
                         "units": "Units", "unit_codes": "Unit codes",
                         "bank_on_file": st.column_config.CheckboxColumn(
                             "Bank details", help="Ticked = on file. The details are only shown when editing.")})

    st.divider()
    ll = picker("Landlord", shown.reset_index(drop=True), lambda r: f"{r['code']} · {r['name']}", "ll_pick",
                can_edit)
    if ll:
        units = query("""SELECT u.code, u.status, tn.name AS tenant, t.monthly_rent, t.end_date
                         FROM units u
                         LEFT JOIN tenancies t ON t.unit_id = u.id AND t.status = 'active'
                         LEFT JOIN tenants tn ON tn.id = t.tenant_id
                         WHERE u.landlord_id = %s ORDER BY u.code""", (int(ll["id"]),))
        st.markdown(f"**Units owned by {ll['name']}** ({len(units)})")
        if units.empty:
            st.caption("No units linked to this landlord.")
        else:
            st.dataframe(units, hide_index=True, width="stretch", column_config={
                "code": "Unit", "status": "Status", "tenant": "Tenant (active tenancy)",
                "monthly_rent": st.column_config.NumberColumn("Rent", format="RM %.2f"),
                "end_date": st.column_config.DateColumn("Tenancy ends", format="DD/MM/YYYY")})

    if can_edit:
        st.markdown("##### " + ("Add a landlord" if ll is None else "Edit details"))
        b = {"bank": None, "account_name": None, "account_no": None}
        if ll:
            got = query("SELECT bank, account_name, account_no FROM landlord_bank WHERE landlord_id = %s",
                        (int(ll["id"]),))
            if not got.empty:
                b = got.iloc[0].to_dict()
        with st.form(f"landlord_{ll['id'] if ll else 'new'}"):
            c1, c2 = st.columns(2)
            code = c1.text_input("Landlord code", value=ll["code"] if ll else "", disabled=ll is not None,
                                 help="Short and permanent, e.g. L130")
            name = c2.text_input("Name *", value=(ll or {}).get("name") or "")
            phone = c1.text_input("Phone", value=(ll or {}).get("phone") or "")
            email = c2.text_input("Email", value=(ll or {}).get("email") or "")
            fee = c1.number_input("Management fee %", min_value=0.0, max_value=100.0, step=0.5,
                                  value=float((ll or {}).get("management_fee_pct") or 10))
            notes = st.text_area("Notes (no IC numbers)", value=(ll or {}).get("notes") or "")
            st.markdown("**🔒 Bank details** (for paying the landlord; never shown in the table)")
            b1, b2, b3 = st.columns(3)
            bank = b1.text_input("Bank", value=b["bank"] or "", placeholder="e.g. Maybank")
            acc_name = b2.text_input("Account name", value=b["account_name"] or "")
            acc_no = b3.text_input("Account number", value=b["account_no"] or "")
            if st.form_submit_button("Save landlord", type="primary"):
                after = {"name": name.strip(), "phone": norm_phone(phone), "email": opt(email),
                         "management_fee_pct": fee, "notes": opt(notes)}
                new_bank = {"bank": opt(bank), "account_name": opt(acc_name),
                            "account_no": opt("".join(acc_no.split()))}
                mask = lambda v: f"…{v[-4:]}" if v else None          # audit keeps the last 4 digits only
                if not after["name"] or (ll is None and not code.strip()):
                    st.error("Code and name are required.")
                else:
                    try:
                        with pool().connection() as conn, conn.transaction():
                            if ll is None:
                                lid = conn.execute("""
                                    INSERT INTO landlords (code, name, phone, email, management_fee_pct, notes)
                                    VALUES (%(code)s, %(name)s, %(phone)s, %(email)s, %(management_fee_pct)s,
                                            %(notes)s) RETURNING id""",
                                    {"code": code.strip().upper(), **after}).fetchone()["id"]
                                audit(conn, user["id"], "add_landlord", "landlords", lid, None,
                                      {"code": code.strip().upper(), **after})
                            else:
                                lid = int(ll["id"])
                                conn.execute("""UPDATE landlords SET name = %(name)s, phone = %(phone)s,
                                                    email = %(email)s, management_fee_pct = %(management_fee_pct)s,
                                                    notes = %(notes)s WHERE id = %(id)s""", {**after, "id": lid})
                                audit(conn, user["id"], "edit_landlord", "landlords", lid,
                                      {k: ll[k] for k in after}, after)
                            if new_bank != {k: b[k] for k in new_bank}:
                                conn.execute("""INSERT INTO landlord_bank (landlord_id, bank, account_name, account_no)
                                                VALUES (%s, %s, %s, %s)
                                                ON CONFLICT (landlord_id) DO UPDATE
                                                SET bank = EXCLUDED.bank, account_name = EXCLUDED.account_name,
                                                    account_no = EXCLUDED.account_no""",
                                             (lid, new_bank["bank"], new_bank["account_name"], new_bank["account_no"]))
                                audit(conn, user["id"], "edit_landlord_bank", "landlord_bank", lid,
                                      {"bank": b["bank"], "account_no": mask(b["account_no"])},
                                      {"bank": new_bank["bank"], "account_no": mask(new_bank["account_no"])})
                        st.success("Landlord added." if ll is None else "Landlord saved."); st.rerun()
                    except psycopg.errors.UniqueViolation:
                        st.error(f"Landlord code '{code.strip().upper()}' already exists.")

    if ll and is_admin:
        if int(ll["units"]):
            st.caption(f"To delete this landlord, first move or delete their {int(ll['units'])} unit(s).")
        else:
            delete_button(f"landlord {ll['code']}", f"del_ll_{ll['id']}", """
                WITH b AS (DELETE FROM landlord_bank WHERE landlord_id = %(id)s)
                DELETE FROM landlords WHERE id = %(id)s RETURNING id""", {"id": int(ll["id"])},
                "delete_landlord", "landlords", {"code": ll["code"], "name": ll["name"]})

# ── Tenants ──────────────────────────────────────────────────────────────────
with tenant_tab:
    tenants = query("""SELECT tn.id, tn.name, tn.phone, tn.email, tn.notes,
                              string_agg(u.code, ', ' ORDER BY u.code) FILTER (WHERE t.status = 'active') AS renting,
                              count(t.id) AS tenancies
                       FROM tenants tn
                       LEFT JOIN tenancies t ON t.tenant_id = tn.id
                       LEFT JOIN units u ON u.id = t.unit_id
                       GROUP BY tn.id ORDER BY tn.name""")
    search = st.text_input("Search tenants", placeholder="Name, phone or unit")
    shown = tenants
    if search.strip() and not tenants.empty:
        s = search.strip().lower()
        digits = norm_phone(s) or "~"
        shown = tenants[tenants["name"].str.lower().str.contains(s, regex=False)
                        | tenants["phone"].fillna("").str.contains(digits, regex=False)
                        | tenants["renting"].fillna("").str.lower().str.contains(s, regex=False)]
    st.caption(f"{len(shown)} of {len(tenants)} tenants")
    if not shown.empty:
        st.dataframe(shown[["name", "phone", "email", "renting", "tenancies", "notes"]].fillna(""), hide_index=True,
                     width="stretch", column_config={"name": "Name", "phone": "Phone", "email": "Email",
                                                     "renting": "Renting now", "tenancies": "Tenancies",
                                                     "notes": "Notes"})
    st.divider()
    tn = picker("Tenant", shown.reset_index(drop=True), lambda r: f"{r['name']} · {r['phone'] or 'no phone'}",
                "tenant_pick", can_edit)
    if can_edit:
        st.markdown("##### " + ("Add a tenant" if tn is None else "Edit details"))
        with st.form(f"tenant_{tn['id'] if tn else 'new'}"):
            name = st.text_input("Name *", value=(tn or {}).get("name") or "",
                                 help="Full name exactly as on their IC: bank transfers are matched on it")
            phone = st.text_input("Phone *", value=(tn or {}).get("phone") or "",
                                  help="Any format, e.g. 012-345 6789. Saved as 60123456789 for WhatsApp.")
            email = st.text_input("Email", value=(tn or {}).get("email") or "")
            notes = st.text_area("Notes (no IC numbers)", value=(tn or {}).get("notes") or "")
            if st.form_submit_button("Save tenant", type="primary"):
                after = {"name": name.strip(), "phone": norm_phone(phone), "email": opt(email), "notes": opt(notes)}
                if not after["name"] or not after["phone"]:
                    st.error("Name and phone are required.")
                elif not after["phone"].startswith("60") or not 10 <= len(after["phone"]) <= 13:
                    st.error(f"'{phone}' does not look like a Malaysian mobile number.")
                elif tn is None:
                    if save("add_tenant", "tenants", """
                            INSERT INTO tenants (name, phone, email, notes)
                            VALUES (%(name)s, %(phone)s, %(email)s, %(notes)s) RETURNING id""",
                            after, None, after):
                        st.success(f"Added {after['name']}. Start their tenancy in Register."); st.rerun()
                elif save("edit_tenant", "tenants", """
                        UPDATE tenants SET name = %(name)s, phone = %(phone)s, email = %(email)s, notes = %(notes)s
                        WHERE id = %(id)s RETURNING id""", {**after, "id": int(tn["id"])},
                        {k: tn[k] for k in after}, after):
                    st.success("Tenant saved."); st.rerun()
    if tn and is_admin:
        if int(tn["tenancies"]):
            st.caption("Tenants with tenancies can't be deleted (their payment history is kept).")
        else:
            delete_button(f"tenant {tn['name']}", f"del_tn_{tn['id']}", """
                WITH a AS (DELETE FROM payer_aliases WHERE tenant_id = %(id)s)
                DELETE FROM tenants WHERE id = %(id)s RETURNING id""", {"id": int(tn["id"])},
                "delete_tenant", "tenants", {"name": tn["name"], "phone": tn["phone"]})
