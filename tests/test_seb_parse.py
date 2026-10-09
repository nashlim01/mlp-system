"""SEBCares JSON -> rows (worker/jobs/seb_parse.py). Shapes copied from the live portal; values made up."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from jobs.seb_parse import money, parse_accounts, parse_bill, total_rows  # noqa: E402


def bal(text, value):
    return {"TEXT": text, "WITHD_VAL": value, "CURRENCY": "MYR", "CURRENCY_ISO": "MYR"}


def inv(doc, date, due, start, end, amt, total, rev=""):
    return {"PRINT_DOC": doc, "CONTRACT_ACC": "100000000001", "BUS_PARTNER": "3000000001", "INVOICE_DATE": date,
            "DUE_DATE": due, "CURR_AMT": f"{amt:>10}", "CURR_TOTAL_AMT": f"{total:>10}",
            "START_BILL_PERIOD": start, "END_BILL_PERIOD": end, "PERIOD": date[:7].replace("-", "/"),
            "FILNAME": f"{doc}.pdf", "REVERSAL_FLAG": rev, "BILL_TYPE": "01"}


BILL = {
    "IsContractBelongToCurrentUser": True,
    "ContractSubscription": {"Id": "1200001", "UserId": 0, "ContractId": "100000000001", "Nickname": "Unit A",
                             "IsDeleted": False},
    "CustomerInformations": {
        "EV_AMI_ACCT_VALIDATION": {"INSTALLATION": "7000000001", "MOVE_IN_DATE": "2021-03-22",
                                   "MOVE_OUT_DATE": "9999-12-31", "TARIFF": "DOM", "AMI_FLAG": "N",
                                   "FLAG_CA": "Y", "FLAG_ACTIVE": "Y"},
        "T_ACCOUNT_BALANCES": {"item": {"List": [bal("Open", "4.8500"), bal("Due", "0.0000"),
                                                 bal("Credit", "0.0000"), bal("CSD payt.", "550.0000")]}},
        "T_INVOICE_LIST": {"item": {"List": [
            inv("732000000002", "2026-08-24", "2026-09-14", "2026-07-21", "2026-08-19", "5.15", "84.65"),
            inv("736000000003", "2026-09-22", "2026-10-13", "2026-08-20", "2026-09-19", "4.85", "4.85"),
            inv("736000000004", "2026-09-25", "2026-10-16", "2026-08-20", "2026-09-19", "9.99", "9.99", rev="X"),
        ]}},
        "T_CUST_INFO": {"item": {"List": [{"NAME": "SOMEONE", "ADDR1": "5148-3-53 SOME RESIDENCES", "CITY": "MIRI"}]}},
    },
}


def test_money_handles_padding_and_sap_minus():
    assert money("      4.85") == 4.85
    assert money("1,040.00") == 1040.0
    assert money("12.30-") == -12.30
    assert money("") is None


def test_bill_page_data():
    info = parse_bill(BILL)
    assert info["account_no"] == "100000000001" and info["subscription_id"] == "1200001"
    assert info["nickname"] == "Unit A" and info["tariff"] == "DOM" and info["active"] is True
    assert (info["outstanding"], info["credit"], info["deposit"]) == (4.85, 0.0, 550.0)
    # latest bill that wasn't reversed
    assert (info["amount_due"], info["due_date"]) == (4.85, dt.date(2026, 10, 13))
    assert [b["bill_no"] for b in info["bills"]] == ["736000000004", "736000000003", "732000000002"]
    aug = info["bills"][2]
    assert (aug["amount"], aug["total_due"], aug["period_start"], aug["pdf_name"]) == \
        (5.15, 84.65, dt.date(2026, 7, 21), "732000000002.pdf")
    assert info["bills"][0]["reversed"] and not aug["reversed"]
    assert info["address"] == "5148-3-53 SOME RESIDENCES, MIRI"


def test_accounts_list_found_anywhere_in_the_response():
    listing = {"List": {"List": [
        {"ContractSubscription": {"Id": "1", "ContractId": "100000000001", "Nickname": "Unit A", "IsDeleted": False}},
        {"ContractSubscription": {"Id": "2", "ContractId": "100000000002", "Nickname": "Unit B", "IsDeleted": False}},
        {"ContractSubscription": {"Id": "3", "ContractId": "100000000003", "Nickname": "Gone", "IsDeleted": True}},
        {"ContractSubscription": {"Id": "0", "ContractId": "", "Nickname": "", "IsDeleted": False}},  # empty item
    ]}}
    assert parse_accounts([listing]) == [
        {"subscription_id": "1", "account_no": "100000000001", "nickname": "Unit A"},
        {"subscription_id": "2", "account_no": "100000000002", "nickname": "Unit B"}]


def test_account_list_arrives_in_pages_of_20():
    def page(ids, total=45):
        return {"List": {"List": [{"Id": str(i), "ContractId": f"1000000{i:05d}", "Nickname": f"U{i}",
                                   "TotalRows": total} for i in ids]}}
    pages = [page(range(1, 21)), page(range(21, 41)), page(range(41, 46))]
    assert total_rows(pages[:1]) == 45
    assert len(parse_accounts(pages[:1])) == 20 and len(parse_accounts(pages)) == 45
    assert len(parse_accounts(pages + pages[:1])) == 45                 # a page seen twice isn't doubled
