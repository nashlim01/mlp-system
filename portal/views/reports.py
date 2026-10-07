import datetime as dt
import io

import pandas as pd
import streamlit as st

from db import query

st.title("Reports")

periods = query("SELECT DISTINCT period FROM rent_schedule ORDER BY period DESC")
if periods.empty:
    st.info("No rent lines yet."); st.stop()
months = periods["period"].tolist()
this_month = dt.date.today().replace(day=1)                   # next month exists from the 25th
period = st.selectbox("Month", months, index=months.index(this_month) if this_month in months else 0,
                      format_func=lambda d: f"{d:%B %Y}")

# collected is capped at amount_due per line, so overpayments don't inflate totals (same as Rent Board)
TOTALS = """
    SELECT {group} AS {label},
           count(*)                                              AS units,
           sum(r.amount_due)                                     AS expected,
           sum(LEAST(r.paid, r.amount_due))                      AS collected,
           sum(GREATEST(r.balance, 0))                           AS outstanding,
           count(*) FILTER (WHERE r.status = 'OVERDUE')          AS overdue_units,
           round(100 * sum(LEAST(r.paid, r.amount_due)) / NULLIF(sum(r.amount_due), 0), 1)
                                                                 AS collected_pct
    FROM v_rent_status r
    LEFT JOIN landlords l ON l.id = r.landlord_id
    WHERE r.period = %s
    GROUP BY 1 ORDER BY 1"""

by_staff = query(TOTALS.format(group="r.staff_name", label="staff"), (period,))
by_landlord = query(TOTALS.format(group="COALESCE(l.code || ' · ' || l.name, 'No landlord')",
                                  label="landlord"), (period,))
overdue = query("""SELECT r.unit_code, r.tenant_name, r.tenant_phone, r.staff_name, r.amount_due,
                          r.paid, r.balance, r.due_date, r.days_late, lr.last_reminded_at
                   FROM v_rent_status r
                   LEFT JOIN v_last_reminder lr ON lr.tenancy_id = r.tenancy_id
                   WHERE r.period = %s AND r.status = 'OVERDUE'
                   ORDER BY r.days_late DESC""", (period,))

if not by_staff.empty:
    expected = float(by_staff["expected"].sum())
    collected = float(by_staff["collected"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Expected", f"RM{expected:,.2f}")
    c2.metric("Collected", f"RM{collected:,.2f}")
    c3.metric("Outstanding", f"RM{expected - collected:,.2f}")
    c4.metric("Collection rate", f"{100 * collected / expected:.1f}%" if expected else "-")

st.subheader("By staff")
st.dataframe(by_staff, hide_index=True, width="stretch")
st.subheader("By landlord")
st.dataframe(by_landlord, hide_index=True, width="stretch")
st.subheader("Overdue")
if overdue.empty:
    st.success("Nothing overdue for this month.")
else:
    st.dataframe(overdue, hide_index=True, width="stretch")


def for_excel(df):
    # Excel can't store timezone-aware datetimes
    return df.map(lambda v: v.replace(tzinfo=None) if isinstance(v, dt.datetime) and v.tzinfo else v)


buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xl:
    for name, df in [("By staff", by_staff), ("By landlord", by_landlord), ("Overdue", overdue)]:
        for_excel(df).to_excel(xl, sheet_name=name, index=False)
st.download_button("⬇️ Download Excel", buf.getvalue(), file_name=f"MLP_rent_{period:%Y-%m}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
