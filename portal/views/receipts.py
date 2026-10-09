import streamlit as st

from db import query
from slip_ui import SLIPS_SQL, STATUS, bank_bar, review_card

user = st.session_state.user
st.title("Slips to review")
st.caption("Transfer slips the system couldn't confirm with the bank statement by itself. Slips are uploaded "
           "per tenant on the Rent Board (click the tenant's row).")

bank_bar(user)

pending = query(SLIPS_SQL + " WHERE r.status = 'review' ORDER BY r.uploaded_at")
if pending.empty:
    st.success("Nothing to review. 🎉")
for r in pending.to_dict("records"):
    with st.container(border=True):
        month = f"{r['period']:%B %Y}" if r["period"] is not None else "-"
        st.markdown(f"**{r['unit_code'] or '?'} · {r['tenant_name'] or '?'} · {month}** · "
                    f"uploaded {r['uploaded_at']:%d %b %H:%M}")
        review_card(r, user)

st.subheader("Recent slips")
recent = query(SLIPS_SQL + " WHERE r.status <> 'review' ORDER BY r.uploaded_at DESC LIMIT 50")
if recent.empty:
    st.caption("None yet.")
else:
    recent["result"] = recent["status"].map(STATUS)
    recent["month"] = recent["period"].map(lambda d: f"{d:%b %Y}" if d is not None else "")
    st.dataframe(recent[["uploaded_at", "unit_code", "tenant_name", "month", "amount", "result", "note"]],
                 hide_index=True, width="stretch", column_config={
                     "uploaded_at": st.column_config.DatetimeColumn("Uploaded", format="DD/MM HH:mm"),
                     "unit_code": "Unit", "tenant_name": "Tenant", "month": "Month",
                     "amount": st.column_config.NumberColumn("Amount", format="RM %.2f"),
                     "result": "Result", "note": "Note"})
    st.caption("To undo a recorded slip, open the tenant's row on the Rent Board.")
