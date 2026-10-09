"""Shared bits for the record pages (Personnel, Units, Register): save with audit, pickers, unit status."""
import psycopg
import streamlit as st

from db import audit, pool

NEW = "➕ New"


def opt(v):
    """Empty text input -> NULL."""
    v = (v or "").strip()
    return v or None


def save(action, entity, sql, params, before=None, after=None):
    """Run one write + its audit row in a transaction. Returns the row id, or None on error."""
    user = st.session_state.user
    try:
        with pool().connection() as conn, conn.transaction():
            row = conn.execute(sql, params).fetchone()
            if row is None:
                st.warning("Nothing was saved: the record changed or no longer exists. Reload and retry.")
                return None
            audit(conn, user["id"], action, entity, row["id"], before, after)
            return row["id"]
    except psycopg.errors.UniqueViolation as e:
        msg = str(e)
        if "one_active_tenancy_per_unit" in msg:
            st.error("This unit already has an active tenancy. End it first, or save this one as 'upcoming'.")
        elif "tenants_phone_key" in msg:
            st.error("Another tenant already has this phone number. Search for them under Personnel → Tenants.")
        elif "utility_accounts_type_account_no_key" in msg:
            st.error("That utility account number is already linked to another unit.")
        else:
            st.error(f"Already exists: {msg.splitlines()[0]}")
    except psycopg.errors.CheckViolation as e:
        st.error(f"Invalid value: {str(e).splitlines()[0]}")
    except psycopg.errors.ForeignKeyViolation:
        st.error("Can't delete: other records still use this one (history is kept).")
    return None


def sync_unit_status(conn, unit_id):
    conn.execute("""UPDATE units u SET status = CASE
                      WHEN EXISTS (SELECT 1 FROM tenancies t
                                   WHERE t.unit_id = u.id AND t.status = 'active') THEN 'occupied'
                      ELSE 'vacant' END
                    WHERE u.id = %s AND u.status <> 'inactive'""", (unit_id,))


def picker(label, df, fmt, key, can_edit=True):
    """Select box over a DataFrame with a 'New' option. Returns the chosen row (dict) or None."""
    opts = [NEW] + list(range(len(df))) if can_edit else list(range(len(df)))
    if not opts:
        return None
    k = st.selectbox(label, opts, key=key,
                     format_func=lambda k: NEW if k == NEW else fmt(df.iloc[k]))
    return None if k == NEW else df.iloc[k].to_dict()


def delete_button(label, key, sql, params, action, entity, before):
    """Admin-only delete with a confirm tick. Records with history can't be deleted (foreign keys)."""
    with st.expander(f"🗑️ Delete {label}"):
        sure = st.checkbox(f"Yes, permanently delete {label}", key=f"{key}_sure")
        if st.button("Delete", key=key, disabled=not sure, type="primary"):
            if save(action, entity, sql, params, before, None):
                st.success("Deleted."); st.rerun()
