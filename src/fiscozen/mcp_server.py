"""MCP server exposing Fiscozen invoices and clients.

Guard rails:
- No tool sends anything to SdI. Invoices are created unsent; you send them from the web app.
- Writes are two-step: a call without `confirmation_token` only previews and returns a token
  that fingerprints exactly what would be written. The write happens only when the same
  token comes back AND a fresh preview still matches it (same lines, client, date, number).
- Creating refuses an invoice with the same client and total as one dated within 7 days,
  or as an attempt whose outcome is unknown, because every create burns a fiscal invoice
  number. Overriding needs a preview made with `allow_duplicate=true`, then its token.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .client import CreateOutcomeUnknown, FiscozenClient, FiscozenError
from .config import Settings
from .invoice import (
    InvoiceLine,
    build_client_body,
    fingerprint,
    invoice_token,
    money,
    summarize_preview,
)

ROME = ZoneInfo("Europe/Rome")

mcp = MCPServer(
    "fiscozen",
    instructions=(
        "Fiscozen invoicing. Writes are two-step: call without confirmation_token, show the "
        "preview to the user, and only after their explicit OK call again with the returned "
        "token. Nothing here sends invoices to SdI."
    ),
)
_client: FiscozenClient | None = None
_client_init_lock = threading.Lock()
# Held across duplicate check + create so parallel calls cannot both pass the check
_write_lock = threading.Lock()
# (client_id, total) of creates whose outcome is unknown -> ids of the duplicates already
# known at that moment. The POST may still land, so further creates are gated until a NEW
# duplicate appears (then the duplicate gate takes over) or the user overrides.
_uncertain_creates: dict[tuple[int, str], set[int]] = {}
# Tokens that already performed a write: a replay (e.g. a client retrying a timed-out MCP
# call) must never write again, even when the fresh preview would yield the same token
_used_tokens: set[str] = set()


def client() -> FiscozenClient:
    global _client
    with _client_init_lock:
        if _client is None:
            _client = FiscozenClient(Settings.load())
        return _client


def today_in_italy() -> date:
    return datetime.now(ROME).date()


class Line(BaseModel):
    description: Annotated[str, Field(min_length=1, description="Line text shown on the invoice")]
    amount: Annotated[Decimal, Field(gt=0, description="Unit price in EUR, e.g. 1000.00")]
    quantity: Annotated[Decimal, Field(gt=0)] = Decimal("1")


def _to_lines(lines: list[Line]) -> list[InvoiceLine]:
    return [InvoiceLine(line.description, line.amount, line.quantity) for line in lines]


def _client_id(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    default = client().settings.default_client_id
    if default is None:
        raise FiscozenError("No client_id given and no default_client_id configured")
    return default


def _client_view(counterpart: dict) -> dict:
    return {
        "client_id": counterpart.get("counterpart"),
        "name": counterpart.get("display_name"),
        "vat": counterpart.get("vat"),
        "country": counterpart.get("country"),
        "email": counterpart.get("email"),
        "pec": counterpart.get("pec"),
        "recipient_code": counterpart.get("recipient_code"),
        "invoices": counterpart.get("non_cancelled_invoices_count"),
    }


def _invoice_view(invoice: dict) -> dict:
    # Detail responses have `counterpart`, list items `counterpart_id`; the `customer`
    # snapshot is null even on invoices created by the web app
    return {
        "invoice_id": invoice.get("id"),
        "number": invoice.get("full_id"),
        "date": invoice.get("invoice_date"),
        "amount": invoice.get("amount"),
        "status": invoice.get("status"),
        # TO_SEND = not sent to SdI yet; DELIVERED, NOT_DELIVERED, REJECTED, ... afterwards
        "sdi_status": invoice.get("e_status"),
        "client_id": invoice.get("counterpart_id", invoice.get("counterpart")),
        "client": invoice.get("counterpart_version_display_name"),
    }


def _invoice_preview(
    lines: list[Line],
    client_id: int | None,
    invoice_date: date | None,
    charge_stamp_duty: bool,
    payment_due_date: date | None,
    notes: list[str] | None,
    payment_method_id: int | None,
    allow_duplicate: bool,
) -> tuple[dict, dict, dict, list[dict], int]:
    """(data_params, preview, summary, duplicates, client_id). The token in the summary covers the
    invoice content, the server-computed preview, the payment details, the possible
    duplicates shown, a previous attempt with unknown outcome, and the override flag."""
    fiscozen = client()
    resolved_client = _client_id(client_id)
    resolved_date = invoice_date or today_in_italy()
    data_params, preview, counterpart = fiscozen.prepare_invoice(
        client_id=resolved_client,
        lines=_to_lines(lines),
        invoice_date=resolved_date,
        charge_stamp_duty=charge_stamp_duty,
        payment_due_date=payment_due_date,
        notes=notes,
        payment_method_id=payment_method_id,
    )
    duplicates = fiscozen.find_duplicates(resolved_client, resolved_date, preview["total_amount"])
    duplicate_ids = [d["id"] for d in duplicates]
    attempt = _attempt_key(resolved_client, preview)
    if attempt in _uncertain_creates and set(duplicate_ids) - _uncertain_creates[attempt]:
        del _uncertain_creates[attempt]  # the uncertain invoice has appeared
    uncertain = attempt in _uncertain_creates
    summary = {
        "client": _client_view(counterpart),
        **summarize_preview(preview),
        "possible_duplicates": [_invoice_view(d) for d in duplicates],
        # True = an earlier create for this client and total may have been saved anyway
        "previous_attempt_outcome_unknown": uncertain,
        "allow_duplicate": allow_duplicate,
        "confirmation_token": invoice_token(
            data_params, preview, duplicate_ids, allow_duplicate, uncertain
        ),
    }
    return data_params, preview, summary, duplicates, resolved_client


def _attempt_key(client_id: int, preview: dict) -> tuple[int, str]:
    return client_id, str(money(preview["total_amount"]))


@mcp.tool()
def list_clients(search: str = "") -> list[dict]:
    """List customers, optionally filtered by name."""
    return [_client_view(c) for c in client().list_clients(search)]


@mcp.tool()
def check_vat(country: str, vat_number: str) -> dict:
    """Look up company name and address from a VAT number (ISO country code, e.g. IT, FR)."""
    return client().check_vat(country, vat_number)


@mcp.tool()
def create_client(
    business_name: str,
    country: str,
    address: str,
    postcode: str,
    municipality: str,
    vat_number: str = "",
    typology: Literal["SOCI", "PROF", "PRIV", "PA", "NOPR"] = "SOCI",
    email: str = "",
    pec: str = "",
    recipient_code: str = "",
    tax_code: str = "",
    contact: str = "",
    phone: str = "",
    first_name: str = "",
    last_name: str = "",
    confirmation_token: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Create a customer (two-step: without confirmation_token it only previews).
    typology: SOCI company, PROF professional, PRIV private person (use first_name,
    last_name, tax_code), PA public administration, NOPR non-profit.
    Tip: call check_vat first to get the official name and address."""
    body = build_client_body(
        business_name=business_name,
        country=country,
        address=address,
        postcode=postcode,
        municipality=municipality,
        vat=vat_number,
        typology=typology,
        email=email,
        pec=pec,
        recipient_code=recipient_code,
        tax_code=tax_code,
        contact=contact,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )
    with _write_lock:
        existing = client().find_existing_clients(
            vat=body["vat"], tax_code=body["ssn"], name=business_name, country=body["country"]
        )
        existing_ids = sorted(c.get("counterpart") or 0 for c in existing)
        token = fingerprint("create_client", body, existing_ids, allow_duplicate)
        if confirmation_token != token:
            return {
                "created": False,
                "client": body,
                "existing_with_same_vat": [_client_view(c) for c in existing],
                "matched_on": "VAT number, tax code or name" if existing else None,
                "allow_duplicate": allow_duplicate,
                "confirmation_token": token,
                "next_step": "Show this to the user; after their OK call again with "
                "confirmation_token.",
            }
        if token in _used_tokens:
            return {"created": False, "next_step": "This token was already used."}
        if existing and not allow_duplicate:
            return {
                "created": False,
                "existing_with_same_vat": [_client_view(c) for c in existing],
                "next_step": "A client with the same VAT number, tax code or name exists; use "
                "it instead. Only if the user insists: preview again with allow_duplicate=true "
                "and confirm that.",
            }
        try:
            created = client().create_client(body)
        except BaseException as exc:
            # A 4xx is a definite "not created": the user may fix the data and retry. Any
            # other failure may hide a created client, so this token must not be replayed.
            if not (isinstance(exc, FiscozenError) and exc.status and 400 <= exc.status < 500):
                _used_tokens.add(token)
            raise
        _used_tokens.add(token)
        return {"created": True, "client": _client_view(created)}


@mcp.tool()
def delete_client(client_id: int, confirmation_token: str | None = None) -> dict:
    """Delete a customer (two-step: without confirmation_token it only shows the client)."""
    with _write_lock:
        view = _client_view(client().get_client(client_id))
        token = fingerprint("delete_client", view)
        if confirmation_token != token:
            return {
                "deleted": False,
                "client": view,
                "confirmation_token": token,
                "next_step": "Show this to the user; after their OK call again with "
                "confirmation_token.",
            }
        client().delete_client(client_id)
        return {"deleted": True, "client": view}


@mcp.tool()
def preview_invoice(
    lines: list[Line],
    client_id: int | None = None,
    invoice_date: date | None = None,
    charge_stamp_duty: bool = True,
    payment_due_date: date | None = None,
    notes: list[str] | None = None,
    payment_method_id: int | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Preview an invoice exactly as Fiscozen would compute it (client, number, rows,
    totals, legal notes, payment account). Saves nothing. Legal wording (reverse charge,
    forfettario, stamp duty) is added by Fiscozen: do not put it in notes. client_id defaults
    to the configured client, invoice_date to today (Europe/Rome), and charge_stamp_duty adds
    the 2 EUR stamp row to the customer. Lists possible duplicates (same client and total
    within a week). Returns a confirmation_token for create_invoice with the same arguments."""
    _, _, summary, _, _ = _invoice_preview(
        lines,
        client_id,
        invoice_date,
        charge_stamp_duty,
        payment_due_date,
        notes,
        payment_method_id,
        allow_duplicate,
    )
    return summary


@mcp.tool()
def create_invoice(
    lines: list[Line],
    client_id: int | None = None,
    invoice_date: date | None = None,
    charge_stamp_duty: bool = True,
    payment_due_date: date | None = None,
    notes: list[str] | None = None,
    payment_method_id: int | None = None,
    confirmation_token: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Create an invoice in Fiscozen WITHOUT sending it to SdI (send it from the web app).
    Two-step: without confirmation_token it returns the preview and a token. Show the
    preview to the user; only after their explicit OK call again with the same arguments
    and that token. Each real create consumes the next fiscal invoice number, so never
    retry a failed create blindly: check list_invoices first."""
    fiscozen = client()
    with _write_lock:
        data_params, preview, summary, duplicates, resolved_client = _invoice_preview(
            lines,
            client_id,
            invoice_date,
            charge_stamp_duty,
            payment_due_date,
            notes,
            payment_method_id,
            allow_duplicate,
        )
        if confirmation_token is None:
            return {
                "created": False,
                "preview": summary,
                "next_step": "Show this preview to the user; after their OK call again with "
                "the same arguments and confirmation_token.",
            }
        attempt = _attempt_key(resolved_client, preview)
        if summary["previous_attempt_outcome_unknown"] and not allow_duplicate:
            return {
                "created": False,
                "preview": summary,
                "next_step": "A previous attempt for this client and total ended with an "
                "unknown outcome and may still appear. Check list_invoices; if it is really "
                "missing and the user agrees, preview with allow_duplicate=true and confirm.",
            }
        if confirmation_token in _used_tokens:
            return {
                "created": False,
                "preview": summary,
                "next_step": "This token was already used to create an invoice. Check "
                "list_invoices; a new invoice needs a new preview and the user's OK.",
            }
        if confirmation_token != summary["confirmation_token"]:
            return {
                "created": False,
                "preview": summary,
                "next_step": "The invoice changed since the preview (arguments, date, number "
                "or possible duplicates). Show this new preview and confirm with its token.",
            }
        if duplicates and not allow_duplicate:
            return {
                "created": False,
                "preview": summary,
                "next_step": "Possible duplicates exist. Only if the user explicitly wants a "
                "second invoice: preview again with allow_duplicate=true and confirm that.",
            }
        try:
            created = fiscozen.create_invoice(data_params, preview)
        except BaseException as exc:
            # Only a 4xx FiscozenError is a definite "not created"; anything else may hide
            # a committed invoice, so further creates for this client+total are gated
            definite_rejection = (
                isinstance(exc, FiscozenError)
                and not isinstance(exc, CreateOutcomeUnknown)
                and exc.status is not None
                and 400 <= exc.status < 500
            )
            if not definite_rejection:
                _uncertain_creates[attempt] = {d["id"] for d in duplicates}
                _used_tokens.add(confirmation_token)
            raise
        _uncertain_creates.pop(attempt, None)
        _used_tokens.add(confirmation_token)
    # From here on the invoice exists: verification problems are warnings, never errors,
    # or the caller would believe nothing was created and try again
    warnings = []
    try:
        if created.get("full_id") != preview.get("full_id"):
            warnings.append(
                f"Created as {created.get('full_id')}, not {preview.get('full_id')} as "
                "previewed (another invoice was issued meanwhile)."
            )
        if money(created.get("amount") or 0) != money(preview["total_amount"]):
            warnings.append(
                f"Saved total {created.get('amount')} differs from previewed "
                f"{preview['total_amount']}."
            )
    except Exception:  # noqa: BLE001 - see comment above
        warnings.append("Could not verify the saved number and total against the preview.")
    if warnings:
        return {
            "created": True,
            "invoice": _invoice_view(created),
            "preview": summary,
            "warning": " ".join(warnings) + " Review it in the web app before sending.",
        }
    return {
        "created": True,
        "invoice": _invoice_view(created),
        "preview": summary,
        "next_step": "Review it at https://app.fiscozen.it/app/fatture and send it to "
        "SdI from there; then email the courtesy PDF to the client.",
    }


@mcp.tool()
def list_invoices(year: int | None = None) -> list[dict]:
    """List issued invoices (newest first) with their SdI status, optionally for one year."""
    return [_invoice_view(i) for i in client().list_invoices(year)]


@mcp.tool()
def get_invoice(invoice_id: int) -> dict:
    """Invoice details: header, rows, totals, notes and SdI status."""
    invoice = client().get_invoice(invoice_id)
    summary = summarize_preview(invoice.get("data") or {})
    summary.pop("date_is_valid", None)
    return {**_invoice_view(invoice), **summary}


@mcp.tool()
def delete_invoice(invoice_id: int, confirmation_token: str | None = None) -> dict:
    """Delete an invoice that was NOT sent to SdI and is the latest of its year (so the
    numbering stays gapless). Sent invoices need a credit note. Two-step: without
    confirmation_token it only shows the invoice and whether it can be deleted."""
    fiscozen = client()
    with _write_lock:
        invoice = fiscozen.get_invoice(invoice_id)
        view = _invoice_view(invoice)
        if blocker := fiscozen.deletion_blocker(invoice):
            return {"deleted": False, "invoice": view, "reason": blocker}
        token = fingerprint("delete_invoice", view, invoice.get("updated_at"))
        if confirmation_token != token:
            return {
                "deleted": False,
                "invoice": view,
                "confirmation_token": token,
                "next_step": "Show this to the user; after their OK call again with "
                "confirmation_token.",
            }
        fiscozen.delete_invoice(invoice_id)
        return {"deleted": True, "invoice": view}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
