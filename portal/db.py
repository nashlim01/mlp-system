import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

load_dotenv(Path(__file__).resolve().parent.parent / ".env")   # no-op inside Docker


@st.cache_resource
def pool() -> ConnectionPool:
    return ConnectionPool(
        os.environ["DATABASE_URL"], min_size=1, max_size=5, open=True,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
        check=ConnectionPool.check_connection,      # replaces dropped connections
    )


def query(sql, params=None) -> pd.DataFrame:
    with pool().connection() as conn:
        # dtype=object keeps values as the database returns them: NULL stays None (pandas 3
        # would turn NULL text into NaN, which is truthy), Decimals stay exact
        return pd.DataFrame(conn.execute(sql, params).fetchall(), dtype=object)


def execute(sql, params=None):
    with pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone() if cur.description else None


def audit(conn, staff_id, action, entity, entity_id, before=None, after=None):
    conn.execute("""INSERT INTO audit_log (staff_id, action, entity, entity_id, before, after)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                 (staff_id, action, entity, entity_id, _jsonb(before), _jsonb(after)))


def _jsonb(value):
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str))   # dates, Decimals
