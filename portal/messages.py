import re
from urllib.parse import quote


class _Keep(dict):
    def __missing__(self, key):          # leave unknown {placeholders} as they are
        return "{" + key + "}"


def fill(body, **values) -> str:
    return body.format_map(_Keep(values))


def norm_phone(v):
    """0123456789 / +60 12-345 6789 -> 60123456789 (same rule as scripts/import_register.py)."""
    d = re.sub(r"\D", "", str(v or ""))
    if d.startswith("0"):
        d = "60" + d[1:]
    return d or None


def wa_link(phone, text) -> str:
    """Opens WhatsApp (Web, desktop or phone) with the chat and message ready to send."""
    return f"https://wa.me/{phone}?text={quote(text)}"
