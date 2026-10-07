import streamlit as st

from auth import require_login

st.set_page_config(page_title="MLP Staff Portal", page_icon="🏠", layout="wide")
user = require_login()

pages = [
    st.Page("views/today.py", title="Today", icon="📋", default=True),
    st.Page("views/reminders.py", title="Reminder Queue", icon="💬"),
    st.Page("views/rent_board.py", title="Rent Board", icon="📅"),
    st.Page("views/record_payment.py", title="Record Payment", icon="💵"),
    st.Page("views/tenancy.py", title="Tenancy", icon="🏘️"),
    st.Page("views/register.py", title="Register", icon="🗂️"),
    st.Page("views/utilities.py", title="Utilities", icon="⚡"),
    st.Page("views/reports.py", title="Reports", icon="📊"),
]
if user["role"] == "admin":
    pages += [st.Page("views/bank_matching.py", title="Bank Matching", icon="🏦"),
              st.Page("views/admin.py", title="Admin", icon="⚙️")]

with st.sidebar:
    st.write(f"Signed in as **{user['name']}** ({user['role']})")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()

st.navigation(pages).run()
