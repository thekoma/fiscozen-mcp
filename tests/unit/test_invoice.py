from datetime import date
from decimal import Decimal

import pytest

from fiscozen.invoice import (
    InvoiceLine,
    build_client_body,
    build_create_body,
    build_data_params,
    build_rows,
    invoice_token,
    money,
    summarize_preview,
)
from tests.conftest import load_fixture

# What the web app POSTed on 2026-10-02 (no stamp duty)
WEB_APP_CREATE = load_fixture("invoice_created")["request"]
# What the server accepted on 2026-10-13 for invoice 1/2026 (stamp duty charged)
STAMP_CREATE = load_fixture("invoice_created_with_stamp")["request"]
STAMP_PREVIEW = load_fixture("compute_with_stamp")["response"]
VAT_RC02 = load_fixture("vat_codes")["response"][0]


def web_app_equivalent_params() -> dict:
    """Rebuild the captured invoice from the same inputs the user typed in the web app."""
    rows = build_rows([InvoiceLine("Consultant Role", Decimal("1000.00"))], VAT_RC02)
    return build_data_params(
        counterpart_version_id=2001,
        invoice_date=date(2026, 10, 2),
        vat_ateco="70.20.09",
        payment_method_id=4001,
        rows=rows,
        charge_stamp_duty=False,
        payment_due_date=date(2026, 10, 2),
        notes=["Nota123"],
    )


def stamp_equivalent_params() -> dict:
    line = InvoiceLine(
        "Technical consulting services as Consultant - September 2026", Decimal("1000.00")
    )
    return build_data_params(
        counterpart_version_id=2001,
        invoice_date=date(2026, 10, 13),
        vat_ateco="70.20.09",
        payment_method_id=4001,
        rows=build_rows([line], VAT_RC02),
        charge_stamp_duty=True,
    )


def test_data_params_match_web_app_capture():
    assert web_app_equivalent_params() == WEB_APP_CREATE["_data_params"]


def test_create_body_matches_web_app_capture():
    preview = {"total_amount": "1000.00"}
    assert build_create_body(web_app_equivalent_params(), preview) == WEB_APP_CREATE


def test_stamp_duty_invoice_matches_accepted_request():
    # The real preview put the 2.00 stamp row in auto_rows and a grand total of 1002.00
    assert STAMP_PREVIEW["total_amount"] == "1002.00"
    assert [r["description"] for r in STAMP_PREVIEW["auto_rows"]] == ["Marca da bollo"]
    assert build_create_body(stamp_equivalent_params(), STAMP_PREVIEW) == STAMP_CREATE


def test_rows_keep_only_web_app_vat_keys():
    row = build_rows([InvoiceLine("x", Decimal("1"))], VAT_RC02)[0]
    assert "customer_description" not in row["vat_code"]
    assert row["vat_code"]["code"] == "RC02"


@pytest.mark.parametrize(
    ("quantity", "expected"), [("1", "1"), ("10", "10"), ("100", "100"), ("2.50", "2.5")]
)
def test_quantity_never_in_scientific_notation(quantity, expected):
    row = build_rows([InvoiceLine("Days", Decimal("400"), Decimal(quantity))], VAT_RC02)[0]
    assert row["quantity"] == expected


def test_row_amount_and_total_agree():
    row = build_rows([InvoiceLine("Days", Decimal("400.01"), Decimal("3"))], VAT_RC02)[0]
    assert (row["amount"], row["total"]) == ("400.01", "1200.03")
    assert money(0.1) == Decimal("0.10")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"description": " ", "amount": Decimal("1")},
        {"description": "x", "amount": Decimal("0")},
        {"description": "x", "amount": Decimal("0.004")},
        {"description": "x", "amount": Decimal("400.005")},  # sub-cent price: no silent rounding
        {"description": "x", "amount": Decimal("1"), "quantity": Decimal("0.00001")},
        {"description": "x", "amount": Decimal("0.01"), "quantity": Decimal("0.1")},  # total 0
        {"description": "x", "amount": Decimal("1"), "quantity": Decimal("-1")},
    ],
)
def test_invalid_lines_rejected(kwargs):
    with pytest.raises(ValueError):
        InvoiceLine(**kwargs)


def test_client_body_matches_web_app_capture():
    captured = load_fixture("counterpart_created")["request"]
    body = build_client_body(
        business_name=captured["business_name"],
        country="it",
        address=captured["address"],
        postcode=captured["postcode"],
        municipality=captured["municipality"],
        vat=captured["vat"],
        email=captured["email"],
        pec=captured["pec"],
        contact=captured["contact"],
        phone=captured["phone"],
    )
    assert body == captured


def test_summary_shows_payment_account_and_stamp_row():
    summary = summarize_preview(STAMP_PREVIEW)
    assert summary["number"] == "1/2026"
    assert summary["total"] == "1002.00"
    assert summary["rows"][0]["vat_code"] == "RC02"
    assert summary["automatic_rows"][0]["total"] == "2.00"
    assert summary["payment_method"] == {"method": "Bonifico", "account_ending": "0000"}
    assert any("7-ter" in note or "7 ter" in note for note in summary["notes"])


def test_invoice_token_changes_with_content_or_number():
    params = stamp_equivalent_params()
    token = invoice_token(params, STAMP_PREVIEW)
    assert token == invoice_token(stamp_equivalent_params(), STAMP_PREVIEW)
    assert token != invoice_token({**params, "invoice_date": "2026-10-14"}, STAMP_PREVIEW)
    assert token != invoice_token(params, {**STAMP_PREVIEW, "full_id": "2/2026"})
