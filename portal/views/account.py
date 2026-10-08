import streamlit as st

from auth import MIN_PW, check_password, hash_password, password_problem
from db import audit, pool, query

user = st.session_state.user
st.title("My account")

me = query("SELECT name, email, phone, role FROM staff WHERE id = %s", (user["id"],)).iloc[0]
st.write(f"**{me['name']}** · {me['email']} · role: {me['role']}")

st.subheader("Change password")
with st.form("change_password", clear_on_submit=True):
    current = st.text_input("Current password", type="password")
    new = st.text_input(f"New password (min {MIN_PW} characters)", type="password")
    confirm = st.text_input("Repeat the new password", type="password")
    submitted = st.form_submit_button("Change password")

if submitted:
    row = query("SELECT password_hash FROM staff WHERE id = %s AND active", (user["id"],))
    problem = password_problem(new, confirm)
    if row.empty or not check_password(current, row.iloc[0]["password_hash"]):
        st.error("Your current password is wrong.")
    elif problem:
        st.error(problem)
    elif new == current:
        st.error("The new password must be different from the current one.")
    else:
        with pool().connection() as conn, conn.transaction():
            conn.execute("UPDATE staff SET password_hash = %s WHERE id = %s", (hash_password(new), user["id"]))
            audit(conn, user["id"], "change_own_password", "staff", user["id"])   # never log the hash
        st.success("Password changed. Use the new one next time you log in.")
