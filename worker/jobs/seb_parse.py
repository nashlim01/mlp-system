"""Turns SEBCares JSON (as the portal's own pages receive it) into plain rows. No browser, no DB."""
import datetime as dt


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _items(table):
    """SAP tables arrive as {"item": {"List": [...]}}."""
    return ((table or {}).get("item") or {}).get("List") or []


def money(text):
    """'      4.85' -> 4.85; SAP puts the minus sign at the end ('12.30-')."""
    s = str(text or "").strip().replace(",", "")
    if not s:
        return None
    neg = s.endswith("-") or s.startswith("-")
    v = float(s.strip("-"))
    return -v if neg else v


def day(text):
    s = str(text or "").strip()[:10]
    if not s or s.startswith(("0000", "1900", "9999")):
        return None
    return dt.date.fromisoformat(s)


def parse_accounts(responses):
    """Every subscribed account found in the Accounts page responses: [{subscription_id, account_no, nickname}]."""
    found = {}
    for data in responses:
        for d in _walk(data):
            acct = str(d.get("ContractId") or "").strip()
            if "Nickname" in d and acct and not d.get("IsDeleted"):
                found.setdefault(acct, {"subscription_id": str(d.get("Id") or "").strip(),
                                        "account_no": acct, "nickname": (d.get("Nickname") or "").strip()})
    return list(found.values())


def total_rows(responses):
    """How many accounts the portal says the login has (the list arrives 20 at a time)."""
    rows = [int(d["TotalRows"]) for data in responses for d in _walk(data)
            if str(d.get("TotalRows") or "").strip().isdigit()]
    return max(rows) if rows else None


def _address(ci):
    for table in ("T_CUST_INFO", "T_CUSTOMERLIST"):
        for row in _items(ci.get(table)):
            parts = [str(v).strip() for k, v in row.items()
                     if any(w in k.upper() for w in ("ADDR", "STREET", "HOUSE", "CITY"))
                     and str(v or "").strip()]
            if parts:
                return ", ".join(dict.fromkeys(parts))
    return None


def parse_bill(data):
    """The BillInformation data (the response holding CustomerInformations) -> account, balances, bills."""
    ci = data["CustomerInformations"]
    sub = data.get("ContractSubscription") or {}
    bal = {(b.get("TEXT") or "").strip(): money(b.get("WITHD_VAL")) for b in _items(ci.get("T_ACCOUNT_BALANCES"))}
    meta = ci.get("EV_AMI_ACCT_VALIDATION") or {}
    bills = []
    for i in _items(ci.get("T_INVOICE_LIST")):
        if not str(i.get("PRINT_DOC") or "").strip():
            continue
        bills.append({"bill_no": i["PRINT_DOC"].strip(), "bill_date": day(i.get("INVOICE_DATE")),
                      "due_date": day(i.get("DUE_DATE")), "period_start": day(i.get("START_BILL_PERIOD")),
                      "period_end": day(i.get("END_BILL_PERIOD")), "amount": money(i.get("CURR_AMT")),
                      "total_due": money(i.get("CURR_TOTAL_AMT")),
                      "pdf_name": (i.get("FILNAME") or "").strip() or None,
                      "reversed": bool(str(i.get("REVERSAL_FLAG") or "").strip())})
    bills.sort(key=lambda b: b["bill_date"] or dt.date.min, reverse=True)
    latest = next((b for b in bills if not b["reversed"]), None)
    accounts = {str(i.get("CONTRACT_ACC") or "").strip() for i in _items(ci.get("T_INVOICE_LIST"))} - {""}
    return {
        "account_no": str(sub.get("ContractId") or "").strip() or (accounts.pop() if len(accounts) == 1 else None),
        "subscription_id": str(sub.get("Id") or "").strip() or None,
        "nickname": (sub.get("Nickname") or "").strip() or None,
        "address": _address(ci),
        "tariff": (meta.get("TARIFF") or "").strip() or None,
        "active": {"Y": True, "N": False}.get((meta.get("FLAG_ACTIVE") or "").strip()),
        "outstanding": bal.get("Open"),
        "credit": bal.get("Credit"),
        "deposit": bal.get("CSD payt."),
        "amount_due": latest["amount"] if latest else None,
        "due_date": latest["due_date"] if latest else None,
        "bills": bills,
    }
