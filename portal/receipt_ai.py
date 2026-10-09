"""Read transfer slips and bank-app transaction screenshots with Claude (vision + structured output).

No Streamlit imports: the future WhatsApp intake can call these functions directly.
Account numbers never leave this module in full: the schemas only have room for the last 4 digits.
"""
import base64
import datetime as dt
import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"
FALLBACK = {"extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
            "extra_body": {"fallbacks": "default"}}          # re-run on a fallback model if declined
MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
         "gif": "image/gif", "pdf": "application/pdf"}


class AIError(Exception):
    """The slip could not be read (no key, refusal, API problem)."""


class SlipData(BaseModel):
    is_payment_slip: bool = Field(description="True only for a bank transfer / payment confirmation or receipt")
    successful: bool = Field(description="The slip says the transfer succeeded (not pending/failed)")
    amount: Optional[float] = Field(description="Amount transferred, number only, e.g. 850.00")
    currency: Optional[str] = Field(description="Currency code, e.g. MYR")
    paid_at: Optional[str] = Field(description="Date and time of the transfer as printed, ISO 8601 "
                                               "YYYY-MM-DDTHH:MM (local time, no timezone)")
    sender_name: Optional[str] = Field(description="Payer name ONLY if the slip prints it as the sender "
                                                   "(e.g. 'From'); copied exactly. Otherwise null")
    sender_bank: Optional[str] = Field(description="Bank or app the payment was sent from, e.g. UOB")
    recipient_name: Optional[str] = Field(description="Name of the account that received the money")
    recipient_bank: Optional[str]
    recipient_acct_last4: Optional[str] = Field(description="LAST 4 DIGITS ONLY of the recipient account")
    transfer_mode: Optional[str] = Field(description="e.g. DuitNow, IBG, Instant Transfer, cash deposit")
    reference: Optional[str] = Field(description="Recipient reference / payment description typed by payer")
    txn_ref: Optional[str] = Field(description="Bank transaction reference number")
    duitnow_ref: Optional[str] = Field(description="DuitNow reference number, if any")
    confidence: float = Field(description="0-1: how sure you are that amount, date and names are read correctly")
    notes: Optional[str] = Field(description="Only about legibility: parts cut off, blurry or hard to read")


class BankCredit(BaseModel):
    date: str = Field(description="Transaction date, YYYY-MM-DD")
    description: str = Field(description="The transaction text copied exactly, character for character")
    amount: float = Field(description="Amount received, positive number")


class BankHistory(BaseModel):
    is_bank_history: bool
    credits: list[BankCredit] = Field(description="Incoming money only; leave out every debit/outgoing line")
    notes: Optional[str]


SLIP_PROMPT = """This is a payment slip a tenant sent to a property management company in Malaysia, \
as proof of a rent payment. Read it and fill in the fields.

Rules:
- Copy names, references and numbers exactly as printed; leave a field empty when it isn't shown.
- Never work out or guess who paid or on whose behalf. sender_name is only the name the slip itself \
prints as the sender; many slips don't show one - then leave it empty. The recipient is not the sender.
- notes are only about legibility (cut off, blurry). No opinions about the payment.
- Account numbers: give only the last 4 digits. Never output a full account, card or IC number.
- Dates: Malaysian slips print day before month (08 Oct 2026, 08/10/2026).
- If this is not a payment slip at all, set is_payment_slip to false."""

HISTORY_PROMPT = """This is a screenshot of a bank app's transaction history, taken on {taken} \
(so "Today" = {taken}, "Yesterday" = {yesterday}). List every INCOMING credit (money received, \
usually shown as a positive or green amount). Leave out all debits/outgoing lines, card payments \
and anything with a minus sign.

For each credit give the date (YYYY-MM-DD), the transaction text copied EXACTLY as shown \
(same words, spelling, symbols and order, including any part that is cut off - do not complete, \
shorten or reword it), and the amount. Do not pick out or interpret names or references. \
Never output full account or card numbers. notes are only about legibility (cut off, blurry)."""


def media_type_for(filename: str) -> str:
    return MEDIA.get(filename.rsplit(".", 1)[-1].lower(), "image/jpeg")


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise AIError("AI reading is not set up: add ANTHROPIC_API_KEY to .env (or the Streamlit secrets).")
    return anthropic.Anthropic()


def _content(data: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    kind = "document" if media_type == "application/pdf" else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _ask(data: bytes, media_type: str, prompt: str, schema):
    try:
        resp = _client().messages.parse(
            model=MODEL, max_tokens=4000,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": [_content(data, media_type), {"type": "text", "text": prompt}]}],
            output_format=schema, **FALLBACK)
    except anthropic.AuthenticationError as e:
        raise AIError("The Anthropic API key was rejected; check ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise AIError("AI service is busy (rate limit); try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"AI service error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Could not reach the AI service; check the internet connection.") from e
    if resp.stop_reason == "refusal":
        raise AIError("The AI declined to read this file.")
    if resp.parsed_output is None:
        raise AIError(f"The AI reply could not be read (stop reason: {resp.stop_reason}).")
    return resp.parsed_output


def read_slip(data: bytes, media_type: str) -> SlipData:
    slip = _ask(data, media_type, SLIP_PROMPT, SlipData)
    if slip.recipient_acct_last4:                              # belt and braces: keep 4 digits only
        digits = "".join(ch for ch in slip.recipient_acct_last4 if ch.isdigit())
        slip.recipient_acct_last4 = digits[-4:] or None
    return slip


def read_bank_history(data: bytes, media_type: str, taken_on: dt.date) -> BankHistory:
    prompt = HISTORY_PROMPT.format(taken=f"{taken_on:%d %b %Y}",
                                   yesterday=f"{taken_on - dt.timedelta(days=1):%d %b %Y}")
    hist = _ask(data, media_type, prompt, BankHistory)
    hist.credits = [c for c in hist.credits if c.amount and c.amount > 0]
    return hist

