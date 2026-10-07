import bcrypt
import streamlit as st

from db import query


def _login_form():
    st.title("MLP Staff Portal")
    with st.form("login"):
        email = st.text_input("Email").strip().lower()
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        df = query("""SELECT id, name, role, password_hash FROM staff
                      WHERE lower(email) = %s AND active""", (email,))
        if not df.empty:
            r = df.iloc[0]
            if r["password_hash"] and bcrypt.checkpw(pw.encode(), r["password_hash"].encode()):
                st.session_state.user = {"id": int(r["id"]), "name": r["name"], "role": r["role"]}
                st.rerun()
        st.error("Wrong email or password.")


def require_login() -> dict:
    if "user" not in st.session_state:
        _login_form()
        st.stop()
    return st.session_state.user
