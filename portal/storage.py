import functools
import os
import uuid
from pathlib import Path

from supabase import create_client

BUCKET = "receipts"
# RECEIPT_STORAGE=local keeps files in raw/receipts/ (git-ignored) instead of Supabase Storage,
# for local demos that must not touch the live project.
LOCAL_DIR = Path(__file__).resolve().parent.parent / "raw" / "receipts"


def _local() -> bool:
    return os.environ.get("RECEIPT_STORAGE", "").lower() == "local"


@functools.lru_cache(maxsize=1)
def _client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def upload_file(path: str, data: bytes, content_type: str) -> str:
    if _local():
        target = LOCAL_DIR / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    else:
        _client().storage.from_(BUCKET).upload(path, data, {"content-type": content_type})
    return path


def upload_receipt(unit_code, period, uploaded_file) -> str:
    ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
    path = f"{unit_code}/{period:%Y-%m}/{uuid.uuid4().hex[:12]}.{ext}"
    return upload_file(path, uploaded_file.getvalue(), uploaded_file.type)


def receipt_bytes(path) -> bytes:
    """The file itself, for showing it inside the portal (works for both storage modes)."""
    if _local():
        return (LOCAL_DIR / path).read_bytes()
    return _client().storage.from_(BUCKET).download(path)


def receipt_link(path, seconds=300):
    """Short-lived link; None in local mode (show receipt_bytes instead)."""
    if _local():
        return None
    res = _client().storage.from_(BUCKET).create_signed_url(path, seconds)
    return res.get("signedURL") or res.get("signedUrl")
