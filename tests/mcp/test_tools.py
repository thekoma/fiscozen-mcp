import asyncio
import json
from datetime import UTC, date

import httpx
import pytest

from fiscozen import mcp_server
from tests.conftest import load_fixture
from tests.unit.test_client import INVOICE_LIST, UNSENT, mock_invoice_flow

LINES = [mcp_server.Line(description="Consulting - September 2026", amount="1000.00")]
DAY = date(2026, 10, 13)


@pytest.fixture(autouse=True)
def use_test_client(logged_in_client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client", logged_in_client)
    monkeypatch.setattr(mcp_server, "_uncertain_creates", {})
    monkeypatch.setattr(mcp_server, "_used_tokens", set())


def mock_invoice_list(api, invoices):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": invoices})
    )


# ---- invoices ----------------------------------------------------------------------------


def test_preview_shows_client_payment_and_token(api):
    mock_invoice_flow(api)
    summary = mcp_server.preview_invoice(LINES, invoice_date=DAY)
    assert summary["client"]["name"] == "ACME EUROPE SAS"
    assert summary["total"] == "1002.00"
    assert summary["payment_method"]["method"] == "Bonifico"
    assert summary["confirmation_token"]


def test_create_without_token_only_previews(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/")
    result = mcp_server.create_invoice(LINES, invoice_date=DAY)
    assert result["created"] is False
    assert result["preview"]["confirmation_token"]
    assert not create_route.called


def test_create_with_matching_token_creates_once(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(return_value=httpx.Response(201, json=UNSENT))
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert result["created"] is True
    assert result["invoice"]["number"] == "1/2026"
    assert result["invoice"]["sdi_status"] == "TO_SEND"
    assert create_route.call_count == 1


def test_token_from_a_different_invoice_is_rejected(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/")
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    changed = [mcp_server.Line(description="Something else", amount="1000.00")]
    result = mcp_server.create_invoice(changed, invoice_date=DAY, confirmation_token=token)
    assert result["created"] is False
    assert "changed since the preview" in result["next_step"]
    assert not create_route.called


def test_guessed_token_is_rejected(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/")
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token="yes")
    assert result["created"] is False
    assert not create_route.called


def test_duplicate_shown_in_preview_and_blocks_create(api):
    mock_invoice_flow(api, invoices=INVOICE_LIST)
    create_route = api.post("/api/v1/invoices/")
    preview = mcp_server.preview_invoice(LINES, invoice_date=DAY)
    [duplicate] = preview["possible_duplicates"]
    assert (duplicate["number"], duplicate["sdi_status"]) == ("1/2026", "DELIVERED")
    result = mcp_server.create_invoice(
        LINES, invoice_date=DAY, confirmation_token=preview["confirmation_token"]
    )
    assert result["created"] is False
    assert not create_route.called


def test_duplicate_override_needs_its_own_preview(api):
    mock_invoice_flow(api, invoices=INVOICE_LIST)
    create_route = api.post("/api/v1/invoices/").mock(return_value=httpx.Response(201, json=UNSENT))
    plain_token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    # Reusing the token of the plain preview with the override flag must not work
    sneaky = mcp_server.create_invoice(
        LINES, invoice_date=DAY, confirmation_token=plain_token, allow_duplicate=True
    )
    assert sneaky["created"] is False
    assert not create_route.called
    override = mcp_server.preview_invoice(LINES, invoice_date=DAY, allow_duplicate=True)
    assert override["allow_duplicate"] is True and override["possible_duplicates"]
    result = mcp_server.create_invoice(
        LINES,
        invoice_date=DAY,
        confirmation_token=override["confirmation_token"],
        allow_duplicate=True,
    )
    assert result["created"] is True
    assert create_route.call_count == 1


def test_duplicate_found_across_midnight(api):
    # Same client and total, dated the day before: a retry after midnight
    mock_invoice_flow(api, invoices=INVOICE_LIST)
    preview = mcp_server.preview_invoice(LINES, invoice_date=date(2026, 10, 14))
    assert preview["possible_duplicates"][0]["number"] == "1/2026"


def test_create_without_date_uses_italian_today(api, monkeypatch):
    mock_invoice_flow(api)
    monkeypatch.setattr(mcp_server, "today_in_italy", lambda: date(2026, 10, 14))
    summary = mcp_server.preview_invoice(LINES)
    # compute_data receives the resolved date
    sent = [c.request for c in api.calls if c.request.url.path.endswith("compute_data/")]
    assert json.loads(sent[-1].content)["invoice_date"] == "2026-10-14"
    assert summary["confirmation_token"]


def test_default_date_is_italian_not_utc(monkeypatch):
    from datetime import datetime as real_datetime

    class LateEveningUtc(real_datetime):
        @classmethod
        def now(cls, tz=None):
            # 23:30 UTC on Oct 13 is already Oct 14 in Rome (UTC+2)
            return real_datetime(2026, 10, 13, 23, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(mcp_server, "datetime", LateEveningUtc)
    assert mcp_server.today_in_italy() == date(2026, 10, 14)


def test_list_invoices_reports_real_sdi_status(api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(return_value=httpx.Response(200, json=INVOICE_LIST))
    [invoice] = mcp_server.list_invoices(2026)
    assert invoice["sdi_status"] == "DELIVERED"
    assert invoice["client_id"] == 1001
    assert invoice["client"] == "ACME EUROPE SAS"


def test_delete_invoice_needs_token(api):
    mock_invoice_list(api, [{"invoice_number": 1, "invoice_date": "2026-10-13"}])
    api.get("/api/v1/invoices/3001/").mock(return_value=httpx.Response(200, json=UNSENT))
    delete_route = api.delete("/api/v1/invoices/3001/").mock(return_value=httpx.Response(204))
    first = mcp_server.delete_invoice(3001)
    assert first["deleted"] is False
    assert not delete_route.called
    assert mcp_server.delete_invoice(3001, confirmation_token="guess")["deleted"] is False
    second = mcp_server.delete_invoice(3001, confirmation_token=first["confirmation_token"])
    assert second["deleted"] is True
    assert delete_route.call_count == 1


def test_delete_delivered_invoice_explains_credit_note(api):
    api.get("/api/v1/invoices/3001/").mock(
        return_value=httpx.Response(200, json=load_fixture("invoice_delivered")["response"])
    )
    result = mcp_server.delete_invoice(3001)
    assert result["deleted"] is False
    assert "credit note" in result["reason"]
    assert "confirmation_token" not in result


# ---- clients -----------------------------------------------------------------------------


def mock_client_list(api, clients):
    api.get("/api/v1/counterparts/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": clients})
    )


CLIENT_ARGS = dict(
    business_name="ACME SRL",
    country="IT",
    address="Via Roma 1",
    postcode="20100",
    municipality="Milano",
    vat_number="12345678901",
)


def test_create_client_two_step_with_captured_shape(api):
    mock_client_list(api, [])
    create_route = api.post("/api/v1/counterparts/").mock(
        return_value=httpx.Response(201, json=load_fixture("counterpart_created")["response"])
    )
    first = mcp_server.create_client(**CLIENT_ARGS)
    assert first["created"] is False
    assert not create_route.called
    second = mcp_server.create_client(**CLIENT_ARGS, confirmation_token=first["confirmation_token"])
    assert second["created"] is True
    sent = json.loads(create_route.calls.last.request.content)
    assert sent.keys() == load_fixture("counterpart_created")["request"].keys()


def test_italian_vat_prefix_matches_bare_stored_vat(api):
    # The web app stores Italian VAT numbers bare (captured counterpart_created)
    mock_client_list(api, [{"counterpart": 9, "display_name": "IBM", "vat": "10914660153"}])
    preview = mcp_server.create_client(**{**CLIENT_ARGS, "vat_number": "IT10914660153"})
    assert preview["client"]["vat"] == "10914660153"
    assert preview["existing_with_same_vat"][0]["client_id"] == 9


def test_get_invoice_and_check_vat_tools(api):
    api.get("/api/v1/invoices/3001/").mock(
        return_value=httpx.Response(200, json=load_fixture("invoice_delivered")["response"])
    )
    invoice = mcp_server.get_invoice(3001)
    assert (invoice["number"], invoice["sdi_status"], invoice["total"]) == (
        "1/2026",
        "DELIVERED",
        "1002.00",
    )
    api.get("/api/v1/vat/check/").mock(
        return_value=httpx.Response(200, json={"name": "IBM ITALIA S.P.A", "vat": "10914660153"})
    )
    assert mcp_server.check_vat("it", "10914660153")["name"] == "IBM ITALIA S.P.A"
    assert api.calls.last.request.url.params["country"] == "IT"


def test_create_client_refuses_existing_vat(api):
    mock_client_list(api, [{"counterpart": 3, "display_name": "ACME", "vat": "IT 12345678901"}])
    create_route = api.post("/api/v1/counterparts/")
    first = mcp_server.create_client(**{**CLIENT_ARGS, "vat_number": "IT12345678901"})
    assert first["existing_with_same_vat"][0]["client_id"] == 3
    second = mcp_server.create_client(
        **{**CLIENT_ARGS, "vat_number": "IT12345678901"},
        confirmation_token=first["confirmation_token"],
    )
    assert second["created"] is False
    assert not create_route.called


def test_delete_client_needs_token(api):
    api.get("/api/v1/counterparts/5/").mock(
        return_value=httpx.Response(200, json={"counterpart": 5, "display_name": "Old"})
    )
    delete_route = api.delete("/api/v1/counterparts/5/").mock(return_value=httpx.Response(204))
    first = mcp_server.delete_client(5)
    assert first["deleted"] is False
    assert not delete_route.called
    assert mcp_server.delete_client(5, confirmation_token=first["confirmation_token"])["deleted"]
    assert delete_route.call_count == 1


def test_no_tool_can_send_to_sdi():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "create_invoice" in names
    assert not any("send" in name or "sdi" in name for name in names)


def test_retry_after_unknown_outcome_is_refused(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(side_effect=httpx.ReadTimeout("slow"))
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError, match="outcome unknown"):
        mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    # The server may still commit the first POST: same token again must not POST
    retry = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert retry["created"] is False
    assert "unknown outcome" in retry["next_step"]
    assert create_route.call_count == 1


def test_number_mismatch_after_create_is_flagged(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(
        return_value=httpx.Response(201, json={**UNSENT, "full_id": "2/2026"})
    )
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert result["created"] is True
    assert "2/2026" in result["warning"]
    assert create_route.call_count == 1


def test_create_client_override_path(api):
    mock_client_list(api, [{"counterpart": 3, "display_name": "ACME", "vat": "12345678901"}])
    create_route = api.post("/api/v1/counterparts/").mock(
        return_value=httpx.Response(201, json=load_fixture("counterpart_created")["response"])
    )
    plain = mcp_server.create_client(**CLIENT_ARGS)
    sneaky = mcp_server.create_client(
        **CLIENT_ARGS, confirmation_token=plain["confirmation_token"], allow_duplicate=True
    )
    assert sneaky["created"] is False
    override = mcp_server.create_client(**CLIENT_ARGS, allow_duplicate=True)
    assert override["allow_duplicate"] is True
    done = mcp_server.create_client(
        **CLIENT_ARGS, confirmation_token=override["confirmation_token"], allow_duplicate=True
    )
    assert done["created"] is True
    assert create_route.call_count == 1


def test_override_token_dies_after_unknown_outcome(api):
    mock_invoice_flow(api, invoices=INVOICE_LIST)
    create_route = api.post("/api/v1/invoices/").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(201, json=UNSENT)]
    )
    override = mcp_server.preview_invoice(LINES, invoice_date=DAY, allow_duplicate=True)
    token = override["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError, match="outcome unknown"):
        mcp_server.create_invoice(
            LINES, invoice_date=DAY, confirmation_token=token, allow_duplicate=True
        )
    retry = mcp_server.create_invoice(
        LINES, invoice_date=DAY, confirmation_token=token, allow_duplicate=True
    )
    assert retry["created"] is False
    assert retry["preview"]["previous_attempt_outcome_unknown"] is True
    assert create_route.call_count == 1


def test_5xx_marks_attempt_uncertain_and_success_clears_it(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(
        side_effect=[httpx.Response(502, text="bad gateway"), httpx.Response(201, json=UNSENT)]
    )
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError, match="outcome unknown"):
        mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert mcp_server._uncertain_creates
    override = mcp_server.preview_invoice(LINES, invoice_date=DAY, allow_duplicate=True)
    assert override["previous_attempt_outcome_unknown"] is True
    done = mcp_server.create_invoice(
        LINES,
        invoice_date=DAY,
        confirmation_token=override["confirmation_token"],
        allow_duplicate=True,
    )
    assert done["created"] is True
    assert not mcp_server._uncertain_creates
    assert create_route.call_count == 2


def test_4xx_rejection_does_not_mark_attempt_uncertain(api):
    mock_invoice_flow(api)
    api.post("/api/v1/invoices/").mock(return_value=httpx.Response(400, json={"x": ["bad"]}))
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError):
        mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert not mcp_server._uncertain_creates


def test_amount_mismatch_after_create_is_flagged(api):
    mock_invoice_flow(api)
    api.post("/api/v1/invoices/").mock(
        return_value=httpx.Response(201, json={**UNSENT, "amount": "1000.00"})
    )
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert "1000.00" in result["warning"]


def test_changed_bank_account_invalidates_token(api):
    stamp = load_fixture("compute_with_stamp")["response"]
    mock_invoice_flow(api)
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    # Same payment_method_id, different IBAN behind it
    changed = {
        **stamp,
        "payment_method": {**stamp["payment_method"], "details": "IT11X1111111111111111111111"},
    }
    api.post("/api/v1/invoices/compute_data/").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=load_fixture("compute_empty")["response"]
            if not json.loads(request.content)["rows"]
            else changed,
        )
    )
    create_route = api.post("/api/v1/invoices/")
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert result["created"] is False
    assert not create_route.called


def test_create_client_refuses_existing_tax_code(api):
    mock_client_list(
        api,
        [{"counterpart": 8, "display_name": "Test Person", "vat": "", "ssn": "RSSMRA80A01H501U"}],
    )
    create_route = api.post("/api/v1/counterparts/")
    args = {**CLIENT_ARGS, "vat_number": "", "typology": "PRIV", "tax_code": "rssmra80a01h501u"}
    first = mcp_server.create_client(**args)
    assert first["existing_with_same_vat"][0]["client_id"] == 8
    second = mcp_server.create_client(**args, confirmation_token=first["confirmation_token"])
    assert second["created"] is False
    assert not create_route.called


def test_mcp_create_posts_exactly_the_accepted_body(api):
    accepted = load_fixture("invoice_created_with_stamp")["request"]
    row = accepted["_data_params"]["rows"][0]
    lines = [mcp_server.Line(description=row["description"], amount=row["amount"])]
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(return_value=httpx.Response(201, json=UNSENT))
    token = mcp_server.preview_invoice(lines, invoice_date=DAY)["confirmation_token"]
    mcp_server.create_invoice(lines, invoice_date=DAY, confirmation_token=token)
    assert json.loads(create_route.calls.last.request.content) == accepted


def test_used_invoice_token_cannot_be_replayed(api):
    mock_invoice_flow(api)
    create_route = api.post("/api/v1/invoices/").mock(return_value=httpx.Response(201, json=UNSENT))
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    assert mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)["created"]
    replay = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert replay["created"] is False
    assert create_route.call_count == 1


def test_unverifiable_created_invoice_is_still_reported_created(api):
    mock_invoice_flow(api)
    api.post("/api/v1/invoices/").mock(
        return_value=httpx.Response(201, json={**UNSENT, "amount": "1.002,00"})
    )
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    result = mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert result["created"] is True
    assert "Could not verify" in result["warning"]


def test_uncertain_gate_lifts_once_the_invoice_appears(api):
    total = load_fixture("compute_with_stamp")["response"]["total_amount"]
    listed = {"next": None, "results": []}
    mock_invoice_flow(api, invoices=listed)
    api.post("/api/v1/invoices/").mock(side_effect=httpx.ReadTimeout("slow"))
    token = mcp_server.preview_invoice(LINES, invoice_date=DAY)["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError):
        mcp_server.create_invoice(LINES, invoice_date=DAY, confirmation_token=token)
    assert mcp_server.preview_invoice(LINES, invoice_date=DAY)["previous_attempt_outcome_unknown"]
    # The timed-out invoice was committed after all and now shows up in the list
    client_id = load_fixture("counterpart")["response"]["counterpart"]
    listed["results"].append(
        {
            "id": 99,
            "full_id": "1/2026",
            "invoice_date": DAY.isoformat(),
            "amount": total,
            "counterpart_id": client_id,
            "e_status": "TO_SEND",
        }
    )
    preview = mcp_server.preview_invoice(LINES, invoice_date=DAY)
    assert preview["previous_attempt_outcome_unknown"] is False
    assert preview["possible_duplicates"][0]["invoice_id"] == 99


def test_client_token_replay_without_vat_or_tax_code(api):
    mock_client_list(api, [])
    create_route = api.post("/api/v1/counterparts/").mock(
        return_value=httpx.Response(201, json=load_fixture("counterpart_created")["response"])
    )
    args = {**CLIENT_ARGS, "vat_number": "", "country": "DE", "typology": "PRIV"}
    token = mcp_server.create_client(**args)["confirmation_token"]
    assert mcp_server.create_client(**args, confirmation_token=token)["created"] is True
    assert mcp_server.create_client(**args, confirmation_token=token)["created"] is False
    assert create_route.call_count == 1


def test_client_without_ids_matched_by_name_and_country(api):
    mock_client_list(
        api,
        [{"counterpart": 6, "business_name": "Acme  srl", "country": "IT", "vat": "", "ssn": ""}],
    )
    preview = mcp_server.create_client(**{**CLIENT_ARGS, "vat_number": ""})
    assert preview["existing_with_same_vat"][0]["client_id"] == 6


def test_client_4xx_rejection_keeps_token_usable(api):
    mock_client_list(api, [])
    create_route = api.post("/api/v1/counterparts/").mock(
        side_effect=[
            httpx.Response(400, json={"postcode": ["invalid"]}),
            httpx.Response(201, json=load_fixture("counterpart_created")["response"]),
        ]
    )
    token = mcp_server.create_client(**CLIENT_ARGS)["confirmation_token"]
    with pytest.raises(mcp_server.FiscozenError):
        mcp_server.create_client(**CLIENT_ARGS, confirmation_token=token)
    assert mcp_server.create_client(**CLIENT_ARGS, confirmation_token=token)["created"] is True
    assert create_route.call_count == 2


def test_delete_token_goes_stale_when_invoice_changes(api):
    mock_invoice_list(api, [{"invoice_number": 1, "invoice_date": "2026-10-13"}])
    route = api.get("/api/v1/invoices/3001/").mock(return_value=httpx.Response(200, json=UNSENT))
    delete_route = api.delete("/api/v1/invoices/3001/")
    token = mcp_server.delete_invoice(3001)["confirmation_token"]
    route.mock(return_value=httpx.Response(200, json={**UNSENT, "updated_at": "2099-01-01"}))
    assert mcp_server.delete_invoice(3001, confirmation_token=token)["deleted"] is False
    assert not delete_route.called
