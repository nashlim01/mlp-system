import os
import uuid

import streamlit as st
from supabase import create_client

BUCKET = "receipts"


@st.cache_resource
def _client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def upload_receipt(unit_code, period, uploaded_file) -> str:
    ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
    path = f"{unit_code}/{period:%Y-%m}/{uuid.uuid4().hex[:12]}.{ext}"
    _client().storage.from_(BUCKET).upload(
        path, uploaded_file.getvalue(), {"content-type": uploaded_file.type})
    return path


def receipt_link(path, seconds=300) -> str:
    res = _client().storage.from_(BUCKET).create_signed_url(path, seconds)
    return res.get("signedURL") or res.get("signedUrl")
