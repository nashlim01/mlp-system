import datetime as dt

import streamlit as st

from db import query

user = st.session_state.user
st.title("Rent Board")

periods = query("SELECT DISTINCT period FROM rent_schedule ORDER BY period DESC")
if periods.empty:
    st.info("No monthly rent yet. It appears automatically for active tenancies "
            "(admins: Admin → Monthly rent)."); st.stop()
months = periods["period"].tolist()
this_month = dt.date.today().replace(day=1)                   # next month exists from the 25th
period = st.selectbox("Month", months, index=months.index(this_month) if this_month in months else 0,
                      format_func=lambda d: f"{d:%B %Y}")

staff = query("SELECT id, name FROM staff WHERE active ORDER BY name")
names = ["All"] + staff["name"].tolist()
default = names.index(user["name"]) if user["role"] != "admin" and user["name"] in names else 0
who = st.selectbox("Staff", names, index=default)
statuses = st.multiselect("Status", ["OVERDUE", "PARTIAL", "DUE", "PAID"],
                          default=["OVERDUE", "PARTIAL", "DUE", "PAID"])

df = query("SELECT * FROM v_rent_status WHERE period = %s ORDER BY unit_code", (period,))
if who != "All":
    df = df[df["staff_name"] == who]
for col in ("amount_due", "paid", "balance"):          # Decimal -> float for maths
    if col in df:
        df[col] = df[col].astype(float)

if not df.empty:
    expected = float(df["amount_due"].sum())
    collected = float(df[["paid", "amount_due"]].min(axis=1).sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Expected", f"RM{expected:,.2f}")
    c2.metric("Collected", f"RM{collected:,.2f}")
    c3.metric("Outstanding", f"RM{expected - collected:,.2f}")
    c4.metric("Overdue units", int((df["status"] == "OVERDUE").sum()))
    icons = {"PAID": "🟢", "PARTIAL": "🟠", "DUE": "🟡", "OVERDUE": "🔴"}
    df = df[df["status"].isin(statuses)].copy()
    df["status"] = df["status"].map(lambda s: f"{icons[s]} {s}")
    st.dataframe(df[["unit_code", "tenant_name", "staff_name", "amount_due", "paid",
                     "balance", "due_date", "status", "days_late", "note"]],
                 hide_index=True, width="stretch")
