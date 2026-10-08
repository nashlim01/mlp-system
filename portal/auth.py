import bcrypt
import psycopg
import streamlit as st

from db import execute, query
from messages import norm_phone

MIN_PW = 10


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, hashed) -> bool:
    return bool(hashed) and bcrypt.checkpw(pw.encode(), hashed.encode())


def password_problem(pw: str, confirm: str):
    """Returns an error message, or None if the new password is acceptable."""
    if len(pw) < MIN_PW:
        return f"Password must be at least {MIN_PW} characters."
    if len(pw.encode()) > 72:                     # bcrypt limit
        return "Password is too long (max 72 characters)."
    if pw != confirm:
        return "The two passwords don't match."
    return None


def _login_form():
    with st.form("login"):
        email = st.text_input("Email").strip().lower()
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        df = query("""SELECT id, name, role, password_hash FROM staff
                      WHERE lower(email) = %s AND active""", (email,))
        if not df.empty:
            r = df.iloc[0]
            if check_password(pw, r["password_hash"]):
                st.session_state.user = {"id": int(r["id"]), "name": r["name"], "role": r["role"]}
                st.rerun()
        pending = query("""SELECT 1 FROM staff_requests WHERE lower(email) = %s AND status = 'pending'""",
                        (email,))
        if not pending.empty:
            st.info("Your access request is still waiting for an admin to approve it.")
        else:
            st.error("Wrong email or password.")


def _request_form():
    st.caption("New staff: ask for an account. An admin approves it before you can log in.")
    with st.form("request_access", clear_on_submit=False):
        name = st.text_input("Full name")
        email = st.text_input("Work email").strip().lower()
        phone = st.text_input("Phone (optional)")
        pw = st.text_input(f"Choose a password (min {MIN_PW} characters)", type="password")
        confirm = st.text_input("Repeat the password", type="password")
        submitted = st.form_submit_button("Send request")
    if not submitted:
        return
    problem = password_problem(pw, confirm)
    if not name.strip() or "@" not in email:
        st.error("Enter your name and a valid email.")
    elif problem:
        st.error(problem)
    elif not query("SELECT 1 FROM staff WHERE lower(email) = %s", (email,)).empty:
        st.error("This email already has an account. Log in, or ask an admin to reset your password.")
    else:
        try:
            execute("""INSERT INTO staff_requests (name, email, phone, password_hash)
                       VALUES (%s, %s, %s, %s)""",
                    (name.strip(), email, norm_phone(phone), hash_password(pw)))
            st.success("Request sent. You can log in once an admin approves it.")
        except psycopg.errors.UniqueViolation:
            st.info("A request for this email is already waiting for approval.")


def require_login() -> dict:
    if "user" not in st.session_state:
        st.title("MLP Staff Portal")
        login_tab, request_tab = st.tabs(["Log in", "Request access"])
        with login_tab:
            _login_form()
        with request_tab:
            _request_form()
        st.stop()
    return st.session_state.user
