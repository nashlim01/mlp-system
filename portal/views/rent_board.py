import datetime as dt

import streamlit as st

import receipts_core as rc
from db import pool, query
from slip_ui import bank_bar, slip_panel

user = st.session_state.user
st.title("Rent Board")

periods = query("SELECT DISTINCT period FROM rent_schedule ORDER BY period DESC")
if periods.empty:
    st.info("No monthly rent yet. It appears automatically for active tenancies "
            "(admins: Admin → Monthly rent)."); st.stop()

bank_bar(user)

months = periods["period"].tolist()
this_month = dt.date.today().replace(day=1)                   # next month exists from the 25th
c1, c2, c3 = st.columns([1, 1, 2])
period = c1.selectbox("Month", months, index=months.index(this_month) if this_month in months else 0,
                      format_func=lambda d: f"{d:%B %Y}")
staff = query("SELECT id, name FROM staff WHERE active ORDER BY name")
names = ["All"] + staff["name"].tolist()
default = names.index(user["name"]) if user["role"] != "admin" and user["name"] in names else 0
who = c2.selectbox("Staff", names, index=default)
ALL = ["OVERDUE", "GRACE", "PARTIAL", "DUE", "PAID"]
icons = {"PAID": "🟢 Paid", "PARTIAL": "🟠 Partly paid", "DUE": "🟡 Due", "GRACE": "⏳ In grace",
         "OVERDUE": "🔴 Overdue"}
statuses = c3.multiselect("Status", ALL, default=ALL, format_func=lambda s: icons[s],
                          help="In grace: past the due date but within the grace days (7 unless set otherwise). "
                               "Overdue: after the grace period.")

df = query("""SELECT r.*, sl.slip FROM v_rent_status r
              LEFT JOIN LATERAL (SELECT status AS slip FROM receipts x WHERE x.rent_schedule_id = r.schedule_id
                                 AND x.status IN ('review', 'matched', 'confirmed')
                                 ORDER BY x.uploaded_at DESC LIMIT 1) sl ON TRUE
              WHERE r.period = %s ORDER BY r.unit_code""", (period,))
if who != "All":
    df = df[df["staff_name"] == who]
for col in ("amount_due", "electric_due", "water_due", "total_due", "paid", "balance"):   # Decimal -> float
    if col in df:
        df[col] = df[col].astype(float)

if df.empty:
    st.info("No rent for this selection."); st.stop()

expected = float(df["total_due"].sum())
collected = float(df[["paid", "total_due"]].min(axis=1).sum())
utilities = float((df["electric_due"] + df["water_due"]).sum())
m1, m2, m3, m4 = st.columns(4)
m1.metric("Expected", f"RM{expected:,.2f}", help=f"Rent RM{expected - utilities:,.2f} + utilities RM{utilities:,.2f}")
m2.metric("Collected", f"RM{collected:,.2f}")
m3.metric("Outstanding", f"RM{expected - collected:,.2f}")
m4.metric("Overdue units", int((df["status"] == "OVERDUE").sum()),
          help=f"After the grace period. {int((df['status'] == 'GRACE').sum())} more in grace.")

with pool().connection() as conn:
    hints = rc.bank_hints(conn)                           # money under a tenant's name, not recorded yet
slip_icons = {"review": "🟡 Review", "matched": "✅", "confirmed": "☑️"}
view = df[df["status"].isin(statuses)].reset_index(drop=True)
shown = view.copy()
shown["status"] = shown["status"].map(icons)
shown["slip"] = shown["slip"].map(lambda s: slip_icons.get(s, ""))
shown["bank"] = shown["tenancy_id"].map(
    lambda t: f"💰 RM{sum(float(c['amount']) for c, _ in hints[t]):,.2f}" if t in hints else "")
st.caption("Total = rent + the electricity and water bills issued before the due date. 👆 Click a row to "
           "upload that tenant's transfer slip, or record money already in the bank.")
event = st.dataframe(
    shown[["unit_code", "tenant_name", "staff_name", "amount_due", "electric_due", "water_due", "total_due", "paid",
           "balance", "due_date", "status",
           "days_late", "grace_end", "slip", "bank", "note"]],
    hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key=f"rb_{period}",
    column_config={"unit_code": "Unit", "tenant_name": "Tenant", "staff_name": "Staff",
                   "amount_due": st.column_config.NumberColumn("Rent", format="RM %.2f"),
                   "electric_due": st.column_config.NumberColumn("Electricity", format="RM %.2f",
                                                                 help="SEB bill added to this month"),
                   "water_due": st.column_config.NumberColumn("Water", format="RM %.2f"),
                   "total_due": st.column_config.NumberColumn("Total", format="RM %.2f",
                                                              help="What the tenant should pay this month"),
                   "paid": st.column_config.NumberColumn("Paid", format="RM %.2f"),
                   "balance": st.column_config.NumberColumn("Balance", format="RM %.2f"),
                   "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"), "status": "Status",
                   "days_late": "Days late",
                   "grace_end": st.column_config.DateColumn("Grace until", format="DD/MM/YYYY"),
                   "slip": "Slip", "bank": st.column_config.TextColumn("Bank", help="Money in the bank under this "
                                                                       "tenant's name that isn't recorded yet"),
                   "note": "Note"})

picked = event.selection.rows if event and event.selection else []
if picked:
    st.divider()
    row = view.iloc[picked[0]].to_dict()
    slip_panel(row, user, hints.get(row["tenancy_id"]))
