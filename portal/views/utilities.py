import streamlit as st

from db import execute, query

user = st.session_state.user
st.title("Utilities")
st.caption("Sarawak Energy bills, refreshed by the daily electric_check job (7:00am).")

last = execute("""SELECT started_at AT TIME ZONE 'Asia/Kuching' AS started,
                         finished_at AT TIME ZONE 'Asia/Kuching' AS finished,
                         status, rows_affected, left(error, 500) AS error
                  FROM job_runs WHERE job = 'electric_check'
                  ORDER BY started_at DESC LIMIT 1""")
if last is None:
    st.info("The electricity check has not run yet.")
elif last["status"] == "ok":
    st.success(f"Last check {last['finished']:%d %b %Y, %H:%M}: {last['rows_affected']} account(s) loaded.")
elif last["status"] == "running":
    st.info(f"A check started at {last['started']:%H:%M} is still running.")
else:
    st.error(f"Last check ({last['started']:%d %b %Y, %H:%M}) failed; "
             f"{last['rows_affected'] or 0} account(s) loaded.")
    with st.expander("Error details"):
        st.code(last["error"])

c1, c2 = st.columns(2)
mine = c1.toggle("Only my units", value=user["role"] != "admin")
owing = c2.toggle("Outstanding only", value=False)
conds = ["TRUE"]
if mine:
    conds.append("assigned_staff_id = %(sid)s")
if owing:
    conds.append("outstanding > 0")

df = query(f"""SELECT unit_code, account_no, amount_due, outstanding, due_date,
                      checked_at AT TIME ZONE 'Asia/Kuching' AS checked_at,
                      CASE WHEN checked_at IS NULL THEN '⚪ NOT CHECKED'
                           WHEN COALESCE(outstanding, 0) <= 0 THEN '🟢 PAID'
                           WHEN due_date < today_myt() THEN '🔴 OVERDUE'
                           ELSE '🟡 DUE' END AS status
               FROM v_electric_latest WHERE {' AND '.join(conds)}
               ORDER BY outstanding DESC NULLS LAST, unit_code""", {"sid": user["id"]})
if df.empty:
    st.info("No electricity accounts match. Accounts come from the Utility_Accounts sheet of the register.")
else:
    c1, c2, c3 = st.columns(3)
    c1.metric("Accounts", len(df))
    c2.metric("With outstanding", int((df["outstanding"].fillna(0).astype(float) > 0).sum()))
    c3.metric("Overdue", int(df["status"].str.contains("OVERDUE").sum()))
    st.dataframe(df, hide_index=True, width="stretch")
