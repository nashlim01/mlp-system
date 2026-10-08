import datetime as dt
import io

import altair as alt
import pandas as pd
import streamlit as st

from db import query

st.title("Reports")
st.caption("Rent expected vs collected, from the monthly rent each active tenancy owes. "
           "Use the filters to narrow it down; everything below and the Excel download follow them.")

COLLECTED, OUTSTANDING, EXPECTED = "#2a78d6", "#eb6834", "#8a8a8a"   # validated pair (dataviz palette)
ICONS = {"PAID": "🟢 Paid", "PARTIAL": "🟠 Partly paid", "DUE": "🟡 Due", "OVERDUE": "🔴 Overdue"}
money = lambda label: st.column_config.NumberColumn(label, format="RM %.2f")

periods = query("SELECT DISTINCT period FROM rent_schedule ORDER BY period DESC")
if periods.empty:
    st.info("Nothing to report yet: monthly rent appears once tenancies are entered."); st.stop()
months = periods["period"].tolist()
this_month = dt.date.today().replace(day=1)

# ── Data: the chosen month and the 5 before it (for the trend) ───────────────
c1, c2 = st.columns([1, 3])
period = c1.selectbox("Month", months, index=months.index(this_month) if this_month in months else 0,
                      format_func=lambda d: f"{d:%B %Y}")
raw = query("""SELECT r.schedule_id, r.period, r.unit_code, r.area, r.tenant_name, r.tenant_phone,
                      r.staff_name, COALESCE(l.code || ' · ' || l.name, 'No landlord') AS landlord,
                      r.amount_due, r.paid, r.balance, r.due_date, r.status, r.days_late, r.note,
                      lr.last_reminded_at
               FROM v_rent_status r
               LEFT JOIN landlords l ON l.id = r.landlord_id
               LEFT JOIN v_last_reminder lr ON lr.tenancy_id = r.tenancy_id
               WHERE r.period BETWEEN (%s::date - INTERVAL '5 months')::date AND %s""", (period, period))
for col in ("amount_due", "paid", "balance"):
    raw[col] = raw[col].astype(float)
raw["collected"] = raw[["paid", "amount_due"]].min(axis=1)        # overpayments don't inflate totals
raw["outstanding"] = raw["balance"].clip(lower=0)
raw["area"] = raw["area"].fillna("—")

# ── Filters (one row) ────────────────────────────────────────────────────────
search = c2.text_input("Search unit or tenant", placeholder="e.g. 2587 or Sumerni")
f1, f2, f3, f4 = st.columns(4)
pick = lambda col: sorted(raw[col].dropna().unique().tolist())
staff_f = f1.multiselect("Staff", pick("staff_name"), placeholder="All staff")
area_f = f2.multiselect("Area", pick("area"), placeholder="All areas")
ll_f = f3.multiselect("Landlord", pick("landlord"), placeholder="All landlords")
status_f = f4.multiselect("Status", list(ICONS), format_func=ICONS.get, placeholder="All statuses")

df = raw
if staff_f:
    df = df[df["staff_name"].isin(staff_f)]
if area_f:
    df = df[df["area"].isin(area_f)]
if ll_f:
    df = df[df["landlord"].isin(ll_f)]
if status_f:
    df = df[df["status"].isin(status_f)]
if search.strip():
    s = search.strip().lower()
    df = df[df["unit_code"].str.lower().str.contains(s, regex=False)
            | df["tenant_name"].str.lower().str.contains(s, regex=False)]

month_df = df[df["period"] == period]
prev = (period - dt.timedelta(days=1)).replace(day=1)
prev_df = df[df["period"] == prev]
if month_df.empty:
    st.info("No rent matches these filters for this month."); st.stop()


def totals(d):
    exp, col = d["amount_due"].sum(), d["collected"].sum()
    return {"expected": exp, "collected": col, "outstanding": exp - col,
            "rate": col / exp if exp else 0.0, "overdue": int((d["status"] == "OVERDUE").sum()), "units": len(d)}


t, p = totals(month_df), totals(prev_df) if not prev_df.empty else None
vs = f" vs {prev:%b}"


def rm(v):
    return f"RM{v:,.0f}" if abs(v - round(v)) < 0.005 else f"RM{v:,.2f}"


def delta(k, money=True):
    """Sign first, so Streamlit picks the right arrow: '-RM4,750 vs Sep'."""
    if p is None:
        return None
    d = t[k] - p[k]
    return f"{'-' if d < 0 else '+'}{rm(abs(d)) if money else abs(d)}{vs}"


# ── Headline figures ─────────────────────────────────────────────────────────
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Expected", rm(t["expected"]), delta("expected"), delta_color="off",
          help="Total rent owed for the month by the tenancies shown")
k2.metric("Collected", rm(t["collected"]), delta("collected"),
          help="Paid so far, capped at the amount owed per unit")
k3.metric("Outstanding", rm(t["outstanding"]), delta("outstanding"), delta_color="inverse")
k4.metric("Collected %", f"{t['rate']:.0%}",
          None if p is None else f"{'-' if t['rate'] < p['rate'] else '+'}{abs(t['rate'] - p['rate']) * 100:.0f} pts{vs}")
k5.metric("Overdue units", t["overdue"], delta("overdue", money=False), delta_color="inverse")
st.progress(min(t["rate"], 1.0), text=f"{t['rate']:.0%} of {period:%B} rent collected "
            f"({t['units']} tenancies)")

overview, units_tab, overdue_tab, trend_tab = st.tabs(
    ["📊 Overview", "📋 Unit details", f"🔴 Overdue ({t['overdue']})", "📈 6-month trend"])

# ── Overview: grouped ────────────────────────────────────────────────────────
GROUPS = {"Area": "area", "Staff": "staff_name", "Landlord": "landlord"}
with overview:
    by = st.radio("Group by", list(GROUPS), horizontal=True)
    key = GROUPS[by]
    g = (month_df.groupby(key)
         .agg(units=("schedule_id", "count"), expected=("amount_due", "sum"), collected=("collected", "sum"),
              outstanding=("outstanding", "sum"), overdue=("status", lambda s: int((s == "OVERDUE").sum())))
         .reset_index().rename(columns={key: by}))
    g["collected_pct"] = (g["collected"] / g["expected"]).where(g["expected"] > 0, 0) * 100
    g = g.sort_values("outstanding", ascending=False)
    top = g.head(15)
    st.markdown(f"**Collected vs outstanding by {by.lower()}**" +
                (f" (top 15 of {len(g)} by outstanding; full list below)" if len(g) > 15 else ""))
    long = top.melt(id_vars=[by], value_vars=["collected", "outstanding"], var_name="Measure", value_name="RM")
    long["Measure"] = long["Measure"].str.capitalize()
    long["order"] = (long["Measure"] == "Outstanding").astype(int)          # collected sits at the baseline
    long = long.astype({by: str, "Measure": str, "RM": float})
    chart = (alt.Chart(long).mark_bar()
             .encode(y=alt.Y(f"{by}:N", sort=top[by].tolist(), title=None, scale=alt.Scale(paddingInner=0.3)),
                     x=alt.X("RM:Q", stack="zero", title="RM", axis=alt.Axis(format=",.0f")),
                     color=alt.Color("Measure:N", scale=alt.Scale(domain=["Collected", "Outstanding"],
                                                                  range=[COLLECTED, OUTSTANDING]),
                                     legend=alt.Legend(orient="top", title=None)),
                     order=alt.Order("order:Q"),
                     tooltip=[alt.Tooltip(f"{by}:N"), alt.Tooltip("Measure:N"),
                              alt.Tooltip("RM:Q", format=",.2f")])
             .properties(height=alt.Step(34)))                                   # 34 px per row
    st.altair_chart(chart, width="stretch")
    st.dataframe(g, hide_index=True, width="stretch", column_config={
        "units": st.column_config.NumberColumn("Units"),
        "expected": money("Expected"), "collected": money("Collected"), "outstanding": money("Outstanding"),
        "overdue": st.column_config.NumberColumn("Overdue units"),
        "collected_pct": st.column_config.ProgressColumn("Collected", format="%.0f%%", min_value=0, max_value=100),
    })

# ── Unit details ─────────────────────────────────────────────────────────────
DETAIL_COLS = ["unit_code", "tenant_name", "area", "landlord", "staff_name", "amount_due", "paid",
               "outstanding", "due_date", "status", "days_late", "note"]
detail = month_df.sort_values(["status", "unit_code"])[DETAIL_COLS].copy()
detail["status"] = detail["status"].map(ICONS)
with units_tab:
    st.dataframe(detail, hide_index=True, width="stretch", column_config={
        "unit_code": "Unit", "tenant_name": "Tenant", "area": "Area", "landlord": "Landlord",
        "staff_name": "Staff", "amount_due": money("Rent"),
        "paid": money("Paid"),
        "outstanding": money("Outstanding"),
        "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"), "status": "Status",
        "days_late": "Days late", "note": "Note"})

# ── Overdue ──────────────────────────────────────────────────────────────────
od = month_df[month_df["status"] == "OVERDUE"].sort_values("days_late", ascending=False)
overdue_view = od[["unit_code", "tenant_name", "tenant_phone", "staff_name", "outstanding", "due_date",
                   "days_late", "last_reminded_at"]]
with overdue_tab:
    if od.empty:
        st.success("Nothing overdue for this month.")
    else:
        st.caption("Longest overdue first. Send reminders from the Reminder Queue.")
        st.dataframe(overdue_view, hide_index=True, width="stretch", column_config={
            "unit_code": "Unit", "tenant_name": "Tenant", "tenant_phone": "Phone", "staff_name": "Staff",
            "outstanding": money("Outstanding"),
            "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"), "days_late": "Days late",
            "last_reminded_at": st.column_config.DatetimeColumn("Last reminded", format="DD/MM/YYYY HH:mm")})
        st.page_link("views/reminders.py", label="Open the Reminder Queue →", icon="💬")

# ── Trend ────────────────────────────────────────────────────────────────────
trend = (df.groupby("period").agg(Expected=("amount_due", "sum"), Collected=("collected", "sum"),
                                   units=("schedule_id", "count"), overdue=("status", lambda s: int((s == "OVERDUE").sum())))
         .reset_index().sort_values("period"))
trend["Collected %"] = (trend["Collected"] / trend["Expected"] * 100).round(0)
trend["Month"] = trend["period"].map(lambda d: f"{d:%Y-%m}")
with trend_tab:
    if len(trend) < 2:
        st.info("The trend appears once there are at least two months of rent.")
    else:
        st.markdown("**Expected vs collected per month** (same filters)")
        st.line_chart(trend, x="Month", y=["Expected", "Collected"], color=[EXPECTED, COLLECTED],
                      x_label="", y_label="RM", height=260)
    st.dataframe(trend[["Month", "units", "Expected", "Collected", "Collected %", "overdue"]], hide_index=True,
                 width="stretch", column_config={"units": "Units", "Expected": money("Expected"), "Collected": money("Collected"),
                                                 "Collected %": st.column_config.ProgressColumn(
                                                     "Collected", format="%.0f%%", min_value=0, max_value=100),
                                                 "overdue": "Overdue units"})


# ── Excel (follows the filters) ──────────────────────────────────────────────
def for_excel(d):
    # Excel can't store timezone-aware datetimes
    return d.map(lambda v: v.replace(tzinfo=None) if isinstance(v, dt.datetime) and v.tzinfo else v)


filters = {"Month": f"{period:%B %Y}", "Staff": ", ".join(staff_f) or "All", "Area": ", ".join(area_f) or "All",
           "Landlord": ", ".join(ll_f) or "All", "Status": ", ".join(ICONS[s] for s in status_f) or "All",
           "Search": search.strip() or "-", "Expected (RM)": round(t["expected"], 2),
           "Collected (RM)": round(t["collected"], 2), "Outstanding (RM)": round(t["outstanding"], 2),
           "Collected %": round(t["rate"] * 100, 1), "Overdue units": t["overdue"]}
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xl:
    pd.DataFrame(list(filters.items()), columns=["", "Value"]).to_excel(xl, sheet_name="Summary", index=False)
    for name, key in GROUPS.items():
        (month_df.groupby(key).agg(units=("schedule_id", "count"), expected=("amount_due", "sum"),
                                   collected=("collected", "sum"), outstanding=("outstanding", "sum"))
         .reset_index().rename(columns={key: name}).to_excel(xl, sheet_name=f"By {name.lower()}", index=False))
    for_excel(detail).to_excel(xl, sheet_name="Units", index=False)
    for_excel(overdue_view).to_excel(xl, sheet_name="Overdue", index=False)
    trend.drop(columns="period").to_excel(xl, sheet_name="Trend", index=False)
st.download_button("⬇️ Download Excel (filtered)", buf.getvalue(), file_name=f"MLP_rent_{period:%Y-%m}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
