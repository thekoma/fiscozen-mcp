"""Pure builders for Fiscozen payloads (no I/O, easy to test).

Shapes mirror what the web app sends (captured 2026-10-02 and 2026-10-13):
- POST /api/v1/invoices/compute_data/  body = data params (preview, nothing is saved)
- POST /api/v1/invoices/               body = header fields + `_data_params` (same data params)
- POST /api/v1/counterparts/           body = client fields
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

# Keys the web app keeps on a row's vat_code object (the vat_codes endpoint returns more)
VAT_CODE_KEYS = (
    "code",
    "description",
    "expiration_date",
    "id",
    "kind",
    "law",
    "start_date",
    "value",
    "vat_exempt",
)


@dataclass(frozen=True)
class InvoiceLine:
    description: str
    amount: Decimal  # unit price in EUR, at most 2 decimals
    quantity: Decimal = Decimal("1")  # at most 4 decimals

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("Line description must not be empty")
        amount, quantity = Decimal(str(self.amount)), Decimal(str(self.quantity))
        # Reject instead of rounding: a silently changed price is worse than an error
        if amount != money(amount):
            raise ValueError(f"Unit price {amount} has more than 2 decimals")
        if quantity != quantity.quantize(Decimal("0.0001")):
            raise ValueError(f"Quantity {quantity} has more than 4 decimals")
        object.__setattr__(self, "amount", money(amount))
        object.__setattr__(self, "quantity", quantity)
        if self.amount <= 0 or self.quantity <= 0 or self.total <= 0:
            raise ValueError("Line amount, quantity and total must be positive")

    @property
    def total(self) -> Decimal:
        return money(self.amount * self.quantity)


def money(value: Decimal | str | int | float) -> Decimal:
    """Round to cents. Floats go through str() so 0.1 stays 0.1."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def plain_number(value: Decimal) -> str:
    """'10' not '1E+1': normalize() alone switches to scientific notation."""
    return format(value.normalize(), "f")


def build_rows(lines: list[InvoiceLine], vat_code: dict | None) -> list[dict]:
    """Rows in the web-app format. vat_code=None is only valid for an empty preview."""
    trimmed_vat = {k: vat_code.get(k) for k in VAT_CODE_KEYS} if vat_code else None
    return [
        {
            "amount": str(line.amount),
            "description": line.description.strip(),
            "enasarco_applied": False,
            "ex_enpals_applied": False,
            "key": f"row{index}",
            "quantity": plain_number(line.quantity),
            "total": str(line.total),
            "vat_code": trimmed_vat,
            "welfare_applied": False,
        }
        for index, line in enumerate(lines, start=1)
    ]


def build_data_params(
    *,
    counterpart_version_id: int,
    invoice_date: date,
    vat_ateco: str,
    payment_method_id: int | None,
    rows: list[dict],
    charge_stamp_duty: bool = True,
    payment_due_date: date | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Body for compute_data, and `_data_params` for invoice creation."""
    return {
        "use_last_saved": False,
        "invoice_id": None,
        "counterpart_version_id": counterpart_version_id,
        "add_ex_enpals_contribution": False,
        # Adds the automatic "Marca da bollo" row charged to the customer, when applicable
        "add_tax_stamp_row": charge_stamp_duty,
        "add_withholding_tax": False,
        "currency_code": "EUR",
        "custom_notes": list(notes or []),
        "da_kind": "",
        "e_doc_data": {},
        "enasarco_invoice_kind": "",
        "ex_enpals": {"integrativo": "", "solidarieta": ""},
        "custom_full_id": "",
        "invoice_date": invoice_date.isoformat(),
        "is_e_invoice": True,
        "is_da_invoice": False,
        "no_sts_permission": False,
        "payment_due_date": payment_due_date.isoformat() if payment_due_date else None,
        "payment_method_id": payment_method_id,
        "reason": None,
        "reason_modified_by_bo_at": None,
        "reimbursements": [],
        "analytic_refunds": [],
        "rows": rows,
        "service_kind": "",
        "sts_invoice_type": "",
        "sts_applicable": False,
        "tax_stamp": "",
        "user_logo_id": None,
        "vat_ateco": vat_ateco,
        "update_payment_method_from_db": True,
        "update_user_from_db": True,
        "update_user_logo_from_db": True,
    }


def build_create_body(data_params: dict, preview: dict) -> dict:
    """POST /api/v1/invoices/ body. `amount` is the previewed grand total (stamp row included)."""
    return {
        "self_invoice": False,
        "amount": preview["total_amount"],
        "invoice_date": data_params["invoice_date"],
        "is_e_invoice": data_params["is_e_invoice"],
        "is_da_invoice": False,
        "custom_full_id": "",
        "counterpart_version": data_params["counterpart_version_id"],
        "user_logo": None,
        "da_kind": "",
        "payment_method": data_params["payment_method_id"],
        "reason": None,
        "reason_modified_by_bo_at": None,
        "attachments": [],
        "template_version": "2",
        "vat_ateco": data_params["vat_ateco"],
        "tax_stamp": "",
        "_data_params": data_params,
        "payment_date": None,
    }


def build_client_body(
    *,
    business_name: str,
    country: str,
    address: str,
    postcode: str,
    municipality: str,
    vat: str = "",
    typology: str = "SOCI",
    email: str = "",
    pec: str = "",
    recipient_code: str = "",
    tax_code: str = "",
    contact: str = "",
    phone: str = "",
    first_name: str = "",
    last_name: str = "",
) -> dict:
    """POST /api/v1/counterparts/ body. Province is derived server-side from the municipality.

    The web app stores Italian VAT numbers bare ("10914660153") and foreign ones with their
    prefix ("FR00123456789"), so a leading "IT" is stripped for Italian clients.
    """
    vat = "".join(vat.split()).upper()
    if country.upper() == "IT" and vat.startswith("IT"):
        vat = vat[2:]
    return {
        "address": address,
        "business_name": business_name,
        "contact": contact,
        "counterpart_type": "CUSTOMER",
        "country": country.upper(),
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "municipality": municipality,
        "pec": pec,
        "phone": phone,
        "postcode": postcode,
        "recipient_code": recipient_code,
        "ssn": tax_code.strip().upper(),
        "typology": typology,
        "vat": vat,
    }


def fingerprint(*parts: object) -> str:
    """Short stable hash binding a confirmation to exactly what was shown to the user."""
    canonical = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def invoice_token(
    data_params: dict,
    preview: dict,
    duplicate_ids: list[int] | None = None,
    allow_duplicate: bool = False,
    previous_attempt_uncertain: bool = False,
) -> str:
    """Fingerprint of everything the user approves: our payload, the server's computed
    preview (number, rows incl. automatic ones, totals, notes, due date) and the full
    payment details (not just the last digits shown), plus the duplicate context."""
    shown = summarize_preview(preview)
    shown.pop("date_is_valid", None)
    return fingerprint(
        data_params,
        shown,
        preview.get("payment_method"),
        sorted(duplicate_ids or []),
        allow_duplicate,
        previous_attempt_uncertain,
    )


def summarize_preview(preview: dict) -> dict:
    """The parts of a compute_data response worth showing a human before creating."""

    def row_view(row: dict) -> dict:
        return {
            "description": row.get("description"),
            "unit_price": row.get("amount"),
            "quantity": row.get("quantity"),
            "total": row.get("total"),
            "vat_code": (row.get("vat_code") or {}).get("code"),
        }

    payment = preview.get("payment_method") or {}
    iban = str(payment.get("details") or "")
    return {
        "number": preview.get("full_id"),
        "invoice_date": preview.get("invoice_date"),
        "fiscal_regime": preview.get("fiscal_regime"),
        "rows": [row_view(r) for r in preview.get("complete_rows", [])],
        "automatic_rows": [row_view(r) for r in preview.get("auto_rows", [])],
        "total": preview.get("total_amount"),
        "notes": preview.get("all_notes", []),
        "payment_method": (
            {"method": payment.get("method"), "account_ending": iban[-4:] or None}
            if payment
            else None
        ),
        "payment_due_date": preview.get("payment_due_date"),
        "date_is_valid": preview.get("is_valid_invoice_date"),
    }
