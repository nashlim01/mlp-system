"""Checking a tenant's transfer slip against the company bank statement.

Staff upload a slip for a known tenant and rent month (from the Rent Board). The slip is recorded
when the bank statement shows a credit of the same amount, around the slip's date, under that
tenant's exact name (or an exact name staff saved for them). Anything else goes to review with
plain-language reasons. Pure functions (no database, no AI): tests/test_receipt_match.py.
"""
import datetime as dt
import re
from dataclasses import dataclass, field

# Bank wording that can sit in front of the payer's name in a statement line. Only used to pre-fill
# the "remember this name" box for staff; never for deciding a match.
BANK_NOISE = {"MBB", "CT", "IBG", "IBFT", "GIRO", "DUITNOW", "TRF", "TRANSFER", "FUND", "FUNDS", "INSTANT",
              "CR", "CREDIT", "INWARD", "PAYMENT", "QR", "FPX", "JOMPAY", "CIMB", "PBB", "RHB", "HLB",
              "AMB", "BSN", "UOB", "OCBC", "MAYBANK", "PUBLIC", "BANK"}

DATE_BEFORE, DATE_AFTER = 1, 3          # bank credit may post 1 day before / 3 days after the slip time
MIN_CONFIDENCE = 0.8
MAX_AGE_DAYS = 60
# A name cut off by the bank only counts when enough of it is visible.
CUT_MIN_WORDS, CUT_MIN_CHARS = 2, 12


def words(text) -> list[str]:
    return re.findall(r"[A-Z0-9]+", str(text or "").upper())


def norm_alias(name) -> str:
    """Stored form of a name: every word, upper case, single spaces (nothing dropped)."""
    return " ".join(words(name))


def _parts(text) -> list[list[str]]:
    """Statement lines put the payer and the reference in separate parts: 'MBB CT- NAME * Ref'."""
    return [words(p) for p in str(text or "").split("*")]


def name_in_text(name: str, text: str) -> bool:
    """Strict: every word of `name`, exactly and in order, closing a part of `text`.

    The only allowance is a name cut off by the bank: the part may end inside the name, as long as
    everything visible matches exactly (the last visible word may be the start of the name's word)
    and at least CUT_MIN_WORDS whole words / CUT_MIN_CHARS letters are visible.
    No fuzzy spelling, no skipped words, no abbreviations, no extra words after the name.
    """
    want = words(name)
    if not want:
        return False
    for part in _parts(text):
        for i in range(len(part)):
            k = 0
            while k < len(want) and i + k < len(part) and part[i + k] == want[k]:
                k += 1
            if k == len(want) and i + k == len(part):                 # whole name ends the part
                return True
            if k == len(want) or k < CUT_MIN_WORDS:
                continue
            visible = part[i:]
            cut_on_word = i + k == len(part)                          # "NURUL ASYIKIN BINTI"
            cut_in_word = (i + k == len(part) - 1 and k < len(want)   # "NURUL ASYIKIN BINTI JEF"
                           and want[k].startswith(part[-1]))
            if (cut_on_word or cut_in_word) and len(" ".join(visible)) >= CUT_MIN_CHARS:
                return True
    return False


def payer_text(description) -> str:
    """Best guess of the name part of a statement line, to pre-fill the alias box (staff can edit)."""
    first = str(description or "").split("*")[0]
    ws = words(first)
    while ws and ws[0] in BANK_NOISE:
        ws = ws[1:]
    return " ".join(ws)


def reference_hit(reference: str, text: str) -> bool:
    ref = [w for w in words(reference) if len(w) >= 3]
    return bool(ref) and all(w in words(text) for w in ref)


def parse_paid_at(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(str(value)[:10]), dt.time())
        except ValueError:
            return None


@dataclass
class Decision:
    status: str                                   # 'matched' (record it) | 'review' | 'not_slip'
    flags: list = field(default_factory=list)     # plain-language reasons for review
    bank_txn_id: int | None = None                # the credit found (or the best suggestion)
    link_payment_id: int | None = None            # that credit is already a staff-recorded payment


def bank_candidates(slip: dict, credits: list[dict]) -> list[dict]:
    """Credits of the slip's amount inside the date window, nearest date first."""
    paid = parse_paid_at(slip.get("paid_at"))
    amount = slip.get("amount")
    if paid is None or amount is None:
        return []
    lo, hi = paid.date() - dt.timedelta(days=DATE_BEFORE), paid.date() + dt.timedelta(days=DATE_AFTER)
    out = [c for c in credits
           if abs(float(c["amount"]) - float(amount)) < 0.005 and lo <= c["txn_date"] <= hi]
    return sorted(out, key=lambda c: (not reference_hit(slip.get("reference"), c.get("description")),
                                      abs((c["txn_date"] - paid.date()).days)))


def decide(slip: dict, credits: list[dict], names: list[str], line: dict | None,
           company_last4: set, today: dt.date, bank_up_to: dt.date | None = None) -> Decision:
    """`names`: the tenant's name + exact names staff saved for them.
    `line`: the rent month the slip was uploaded for ({period, balance}); None if unknown.
    `bank_up_to`: latest date in the loaded bank statement (for the explanation only)."""
    if not slip.get("is_payment_slip", True):
        return Decision("not_slip", ["This doesn't look like a payment slip."])

    flags = []
    amount = slip.get("amount")
    paid = parse_paid_at(slip.get("paid_at"))
    if amount is None or amount <= 0:
        flags.append("The amount couldn't be read from the slip.")
    if paid is None:
        flags.append("The date couldn't be read from the slip.")
    elif paid.date() > today:
        flags.append(f"The slip is dated in the future ({paid:%d/%m/%Y}).")
    elif (today - paid.date()).days > MAX_AGE_DAYS:
        flags.append(f"The slip is more than {MAX_AGE_DAYS} days old ({paid:%d/%m/%Y}).")
    if slip.get("successful") is False:
        flags.append("The slip doesn't say the transfer was successful.")
    if (slip.get("confidence") or 0) < MIN_CONFIDENCE:
        flags.append("The slip was hard to read: check the amount and date.")
    last4 = slip.get("recipient_acct_last4")
    if company_last4 and last4 and last4 not in company_last4:
        flags.append(f"Paid into an account ending …{last4}, not the company account.")
    elif company_last4 and not last4:
        flags.append("The slip doesn't show which account received the money.")

    d = Decision("review", flags)
    if amount is None or paid is None:
        return d

    # the rent month it was uploaded for
    if line is not None and amount > float(line["balance"]) + 0.005:
        owed = float(line["balance"])
        flags.append(f"RM{amount:,.2f} is more than what's owed for {line['period']:%B %Y} "
                     f"(RM{owed:,.2f})." if owed > 0 else f"{line['period']:%B %Y} is already fully paid.")

    # the bank: same amount, around the slip's date, under the tenant's exact name
    cands = bank_candidates(slip, credits)
    named = [c for c in cands if any(name_in_text(n, c.get("description")) for n in names)]
    when = f"{paid:%d/%m/%Y}"
    if len(named) == 1 or (len(named) > 1 and reference_hit(slip.get("reference"), named[0].get("description"))
                           and not reference_hit(slip.get("reference"), named[1].get("description"))):
        credit = named[0]
        d.bank_txn_id = credit["id"]
        if credit.get("status") == "matched" and credit.get("matched_payment_id"):
            d.link_payment_id = credit["matched_payment_id"]
            flags[:] = [f for f in flags if "already fully paid" not in f]      # it IS that payment
        elif credit.get("status") != "unmatched":
            flags.append("That bank credit is marked as ignored: check it.")
    elif named:
        flags.append(f"The bank shows {len(named)} payments of RM{amount:,.2f} under this name around {when}: "
                     "pick the right one.")
        d.bank_txn_id = named[0]["id"]
    elif cands:
        flags.append(f"The bank shows RM{amount:,.2f} around {when} from “{cands[0].get('description')}”, "
                     "which isn't this tenant's name. If someone paid for them, pick it and save the name.")
        d.bank_txn_id = cands[0]["id"]
    else:
        upto = f" (statement loaded up to {bank_up_to:%d/%m/%Y})" if bank_up_to else " (no statement loaded yet)"
        flags.append(f"No RM{amount:,.2f} payment in the bank statement around {when}{upto}.")

    if not flags and d.bank_txn_id:
        d.status = "matched"
    return d


# ── Money in the bank with no slip ───────────────────────────────────────────
EARLY_DAYS = 40                         # a credit can pay a month due up to 40 days later (paid in advance)


@dataclass
class BankPick:
    tenancy_id: int | None = None
    line: dict | None = None            # the rent month it pays (oldest unpaid first)
    auto: bool = False                  # record without asking
    reason: str = ""


def bank_pick(credit: dict, people: list[dict]) -> BankPick:
    """Who a bank credit (no slip) belongs to, by the same strict name rule as slips.
    people: [{tenancy_id, names: [tenant name, saved payer names…], lines: [{schedule_id, period, due_date,
    balance}]}] for tenancies with rent still owed. Recorded automatically only when exactly one tenant's
    name is on the credit and the amount is no more than the oldest unpaid month's balance; a part
    payment leaves the remainder owing."""
    owners = [p for p in people if any(name_in_text(n, credit["description"] or "") for n in p["names"])]
    if not owners:
        return BankPick(reason="No tenant's name on this bank credit.")
    if len(owners) > 1:
        return BankPick(reason="More than one tenant's name fits this bank credit.")
    p = owners[0]
    amount = float(credit["amount"])
    lines = sorted((ln for ln in p["lines"] if float(ln["balance"]) > 0.005
                    and credit["txn_date"] >= ln["due_date"] - dt.timedelta(days=EARLY_DAYS)),
                   key=lambda ln: ln["due_date"])
    if not lines:
        return BankPick(p["tenancy_id"], reason="Nothing owed around this date.")
    line = lines[0]
    if amount > float(line["balance"]) + 0.005:
        return BankPick(p["tenancy_id"], line,
                        reason=f"RM{amount:,.2f} is more than the RM{float(line['balance']):,.2f} owed for "
                               f"{line['period']:%B %Y}: check which months it pays.")
    return BankPick(p["tenancy_id"], line, auto=True)


def allocate(amount: float, lines: list[dict]) -> list[tuple[dict, float]]:
    """Split a payment over unpaid months, oldest first; anything left over goes on the last month."""
    out, left = [], round(float(amount), 2)
    open_lines = sorted((ln for ln in lines if float(ln["balance"]) > 0.005), key=lambda ln: ln["due_date"])
    for ln in open_lines:
        if left <= 0.005:
            break
        part = round(min(left, float(ln["balance"])), 2)
        out.append((ln, part))
        left = round(left - part, 2)
    if left > 0.005:
        if out:
            out[-1] = (out[-1][0], round(out[-1][1] + left, 2))
        elif lines:
            out.append((max(lines, key=lambda ln: ln["due_date"]), left))
    return out
