import psycopg
import streamlit as st

from db import audit, pool, query
from messages import norm_phone

user = st.session_state.user
can_edit = user["role"] != "viewer"
is_admin = user["role"] == "admin"
st.title("Landlords")

lls = query("""SELECT l.id, l.code, l.name, l.phone, l.email, l.management_fee_pct, l.notes,
                      count(u.id) AS units,
                      string_agg(u.code, ', ' ORDER BY u.code) AS unit_codes,
                      (b.landlord_id IS NOT NULL AND b.account_no IS NOT NULL) AS bank_on_file
               FROM landlords l
               LEFT JOIN units u ON u.landlord_id = l.id
               LEFT JOIN landlord_bank b ON b.landlord_id = l.id
               GROUP BY l.id, b.landlord_id, b.account_no ORDER BY l.code""")

search = st.text_input("Search", placeholder="Landlord name, code or unit (e.g. Ngui, L001, 2273)")
shown = lls
if search.strip() and not lls.empty:
    s = search.strip().lower()
    shown = lls[lls["name"].str.lower().str.contains(s, regex=False)
                | lls["code"].str.lower().str.contains(s, regex=False)
                | lls["unit_codes"].fillna("").str.lower().str.contains(s, regex=False)]
st.caption(f"{len(shown)} of {len(lls)} landlords")
if not shown.empty:
    st.dataframe(shown[["code", "name", "phone", "email", "management_fee_pct", "units", "unit_codes", "bank_on_file"]],
                 hide_index=True, width="stretch", column_config={
                     "code": "Code", "name": "Name", "phone": "Phone", "email": "Email",
                     "management_fee_pct": st.column_config.NumberColumn("Fee %", format="%.1f%%"),
                     "units": "Units", "unit_codes": "Unit codes",
                     "bank_on_file": st.column_config.CheckboxColumn("Bank details", help="Admins only")})

st.divider()
NEW = "➕ Add a new landlord"
opts = ([NEW] if can_edit else []) + shown["id"].tolist()
if not opts:
    st.stop()
names = dict(zip(lls["id"], lls["code"] + " · " + lls["name"]))
lid = st.selectbox("Landlord", opts, format_func=lambda v: v if v == NEW else names[v],
                   index=1 if can_edit and len(opts) > 1 else 0)
ll = None if lid == NEW else lls[lls["id"] == lid].iloc[0].to_dict()

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
        if st.form_submit_button("Save landlord", type="primary"):
            after = {"name": name.strip(), "phone": norm_phone(phone), "email": email.strip() or None,
                     "management_fee_pct": fee, "notes": notes.strip() or None}
            if not after["name"] or (ll is None and not code.strip()):
                st.error("Code and name are required.")
            else:
                try:
                    with pool().connection() as conn, conn.transaction():
                        if ll is None:
                            new_code = code.strip().upper()
                            new_id = conn.execute("""INSERT INTO landlords (code, name, phone, email,
                                                         management_fee_pct, notes)
                                                     VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                                  (new_code, *after.values())).fetchone()["id"]
                            audit(conn, user["id"], "add_landlord", "landlords", new_id, None,
                                  {"code": new_code, **after})
                        else:
                            conn.execute("""UPDATE landlords SET name = %s, phone = %s, email = %s,
                                                management_fee_pct = %s, notes = %s
                                            WHERE id = %s""", (*after.values(), int(ll["id"])))
                            audit(conn, user["id"], "edit_landlord", "landlords", int(ll["id"]),
                                  {k: ll[k] for k in after}, after)
                    st.success("Landlord saved."); st.rerun()
                except psycopg.errors.UniqueViolation:
                    st.error(f"Landlord code '{code.strip().upper()}' already exists.")

# ── Bank details: admins only (least data) ───────────────────────────────────
if ll and is_admin:
    with st.expander("🔒 Bank details (admins only)"):
        b = query("SELECT bank, account_name, account_no FROM landlord_bank WHERE landlord_id = %s",
                  (int(ll["id"]),))
        b = b.iloc[0].to_dict() if not b.empty else {"bank": None, "account_name": None, "account_no": None}
        with st.form(f"bank_{ll['id']}"):
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
                                 (int(ll["id"]), bank.strip() or None, acc_name.strip() or None,
                                  acc_no.strip() or None))
                    audit(conn, user["id"], "edit_landlord_bank", "landlord_bank", int(ll["id"]),
                          {"bank": b["bank"], "account_no": mask(b["account_no"])},
                          {"bank": bank.strip(), "account_no": mask(acc_no.strip())})
                st.success("Bank details saved."); st.rerun()
elif ll:
    st.caption("🔒 Bank details are kept by admins only.")
