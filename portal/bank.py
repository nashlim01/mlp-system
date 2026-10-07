"""Bank statement CSV parsing and match ranking (used by views/bank_matching.py)."""
import hashlib
import io
import re

import pandas as pd

# Column mapping per bank export (Setup Guide, Appendix A). Download one real statement CSV,
# look at its header row, and add an entry. The key is also stored as account_label, so keep
# it stable: re-uploading under a different label would import the rows again.
#   credit:  column holding money in (debit-only rows are ignored), or
#   amount:  one signed column (+ in, - out), optionally with
#   drcr:    a separate DR/CR marker column; credit_marker is the value meaning money in.
BANK_FORMATS = {
    "Company Bank (generic CSV)": {
        "skiprows": 0,                   # lines above the header row
        "date": "Date",                  # column names exactly as in the file
        "desc": "Description",
        "ref": "Reference",              # or None
        "credit": "Credit",
        "date_format": "%d/%m/%Y",       # or None to let pandas guess (day first)
    },
}
CUSTOM = "Custom: map columns from the file"


def _num(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.upper().str.replace(r"RM|[,\s]", "", regex=True)
    s = s.str.replace(r"^\((.*)\)$", r"-\1", regex=True)          # (123.00) -> -123.00
    s = s.str.replace(r"^(.*)DR$", r"-\1", regex=True)            # 123.00DR -> -123.00
    s = s.str.replace(r"(CR|\+)$", "", regex=True)                # 123.00CR / 123.00+ -> 123.00
    return pd.to_numeric(s, errors="coerce")


def _dates(s: pd.Series, date_format=None) -> pd.Series:
    if date_format:
        return pd.to_datetime(s, format=date_format, errors="coerce")
    if s.str.match(r"\d{4}-\d{2}-\d{2}").all():          # ISO: dayfirst would swap day and month
        return pd.to_datetime(s, format="ISO8601", errors="coerce")
    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def read_csv(file, skiprows):
    raw = file.getvalue() if hasattr(file, "getvalue") else file.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(raw), skiprows=skiprows, dtype=str,
                               encoding=enc).fillna("")
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not read the file as CSV text.")


def load_statement(file, fmt):
    df = read_csv(file, fmt["skiprows"])
    df.columns = [str(c).strip() for c in df.columns]
    needed = [fmt["date"], fmt["desc"]] + [fmt[k] for k in ("ref", "credit", "amount", "drcr") if fmt.get(k)]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"Column(s) {missing} not found. The file's columns are: {list(df.columns)}. "
                         "Check skiprows and the column names in the mapping.")
    if fmt.get("credit"):
        amount = _num(df[fmt["credit"]])
    else:
        amount = _num(df[fmt["amount"]])
        if fmt.get("drcr"):
            is_cr = df[fmt["drcr"]].str.strip().str.upper() == fmt.get("credit_marker", "CR").upper()
            amount = amount.abs().where(is_cr, -amount.abs())
    dates = _dates(df[fmt["date"]].str.strip(), fmt.get("date_format"))
    out = pd.DataFrame({
        "txn_date": dates.dt.date,
        "description": df[fmt["desc"]].str.strip(),
        "reference": df[fmt["ref"]].str.strip() if fmt.get("ref") else "",
        "amount": amount.round(2),
    })
    return out[dates.notna() & (out["amount"] > 0)].reset_index(drop=True)   # credits only


def row_hash(label, r):
    key = f"{label}|{r.txn_date}|{r.amount:.2f}|{r.description}|{r.reference}"
    return hashlib.md5(key.encode()).hexdigest()


def words(name):
    return [w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) >= 3]


def strong(text, unit_code, tenant_name):
    """Unit code, or a 3+ letter word of the tenant's name, appears in the bank text."""
    text = (text or "").lower()
    code = (unit_code or "").lower()
    return bool(code and (code in text or code.replace("-", "") in text.replace(" ", ""))) \
        or any(w in text for w in words(tenant_name))
