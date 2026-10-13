import json
import os
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from fiscozen.client import CreateOutcomeUnknown, FiscozenClient, FiscozenError
from fiscozen.config import ConfigError, Settings
from fiscozen.invoice import InvoiceLine
from tests.conftest import load_fixture, mock_login

INVOICE_LIST = load_fixture("invoice_list")["response"]  # real list with 1/2026 delivered
DELIVERED = load_fixture("invoice_delivered")["response"]
UNSENT = load_fixture("invoice_created_with_stamp")["response"]  # e_status TO_SEND


# ---- auth and session ------------------------------------------------------------------


def test_login_stores_session_with_private_permissions(logged_in_client, settings, api):
    saved = json.loads(settings.session_file.read_text())
    assert saved == {
        "email": "me@example.com",
        "csrftoken": "session-csrf",
        "sessionid": "session-id",
    }
    assert stat.S_IMODE(settings.session_file.stat().st_mode) == 0o600
    login_call = api.calls[1].request
    assert login_call.headers["X-CSRFToken"] == "pre-login-csrf"
    assert json.loads(login_call.content) == {
        "email": "me@example.com",
        "password": "secret-password",
    }


def test_session_file_tightened_even_if_it_existed_world_readable(settings, api):
    settings.session_file.write_text("{}")
    os.chmod(settings.session_file, 0o644)
    mock_login(api)
    FiscozenClient(settings).login()
    assert stat.S_IMODE(settings.session_file.stat().st_mode) == 0o600


def test_unsafe_requests_send_rotated_csrf_and_single_cookie_values(logged_in_client, api):
    route = api.delete("/api/v1/counterparts/5/").mock(return_value=httpx.Response(204))
    logged_in_client.delete_client(5)
    sent = route.calls.last.request
    assert sent.headers["X-CSRFToken"] == "session-csrf"
    assert sent.headers["Cookie"].count("csrftoken=") == 1


def test_failed_login_does_not_leak_password(settings, api):
    api.get("/app/").mock(
        return_value=httpx.Response(200, headers={"set-cookie": "csrftoken=c; Path=/"})
    )
    api.post("/api-auth/browser/v1/auth/login").mock(
        return_value=httpx.Response(400, json={"errors": [{"message": "wrong credentials"}]})
    )
    with pytest.raises(FiscozenError) as error:
        FiscozenClient(settings).login()
    assert "secret-password" not in str(error.value)
    assert not settings.session_file.exists()


def test_expired_session_relogs_in_once_and_retries(settings, api):
    settings.session_file.write_text(
        json.dumps({"email": "me@example.com", "sessionid": "old", "csrftoken": "old"})
    )
    mock_login(api)
    route = api.get("/api/v1/users/self/").mock(
        side_effect=[
            httpx.Response(403, json={"detail": "not authenticated"}),
            httpx.Response(200, json={"id": 5001}),
        ]
    )
    assert FiscozenClient(settings).user_id() == 5001
    assert route.call_count == 2
    assert "sessionid=session-id" in route.calls.last.request.headers["Cookie"]


def test_persistent_403_is_raised_not_looped(logged_in_client, api):
    route = api.get("/api/v1/users/self/").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(FiscozenError):
        logged_in_client.user_id()
    assert route.call_count == 2


def test_cookies_never_sent_off_host(logged_in_client, api):
    for url in ("http://app.fiscozen.it/api/v1/x/", "https://evil.example/api/v1/x/"):
        with pytest.raises(FiscozenError, match="Refusing"):
            logged_in_client.request("GET", url)
    assert not any("evil" in str(call.request.url) for call in api.calls)


def test_pagination_follows_only_path_of_next_link(logged_in_client, api):
    route = api.get("/api/v1/counterparts/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "next": "http://app.fiscozen.it/api/v1/counterparts/?page=2",
                    "results": [{"counterpart": 1}],
                },
            ),
            httpx.Response(200, json={"next": None, "results": [{"counterpart": 2}]}),
        ]
    )
    assert [c["counterpart"] for c in logged_in_client.list_clients()] == [1, 2]
    second = route.calls.last.request.url
    assert (second.scheme, second.host, second.params["page"]) == ("https", "app.fiscozen.it", "2")


# ---- invoices ----------------------------------------------------------------------------


def mock_invoice_flow(api, preview_fixture="compute_with_stamp", invoices=None):
    listed = invoices if invoices is not None else {"next": None, "results": []}
    # Read at call time, so a test can make an invoice "appear" between calls
    api.get("/api/v1/invoices/").mock(side_effect=lambda request: httpx.Response(200, json=listed))
    api.get("/api/v1/counterparts/1001/").mock(
        return_value=httpx.Response(200, json=load_fixture("counterpart")["response"])
    )
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/ateco_choices/").mock(
        return_value=httpx.Response(200, json=load_fixture("ateco_choices")["response"])
    )
    api.get("/api/v1/invoices/customer_blocks/").mock(
        return_value=httpx.Response(200, json=load_fixture("customer_blocks")["response"])
    )
    api.get("/api/v1/payment_methods/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2,
                "next": None,
                "results": [
                    {"id": 364097, "method": "Assegno"},
                    {"id": 4001, "method": "Bonifico"},
                ],
            },
        )
    )
    vat_route = api.get("/api/v1/vat_codes/").mock(
        return_value=httpx.Response(200, json=load_fixture("vat_codes")["response"])
    )
    preview = load_fixture(preview_fixture)["response"]
    compute_route = api.post("/api/v1/invoices/compute_data/").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=load_fixture("compute_empty")["response"]
            if not json.loads(request.content)["rows"]
            else preview,
        )
    )
    return vat_route, compute_route


def test_prepare_invoice_uses_server_chosen_vat_code(logged_in_client, api):
    vat_route, compute_route = mock_invoice_flow(api)
    data_params, preview, client = logged_in_client.prepare_invoice(
        client_id=1001,
        lines=[InvoiceLine("Consulting - September 2026", Decimal("1000.00"))],
        invoice_date=date(2026, 10, 13),
    )
    # The VAT code comes from the filters of the empty preview, not from a guess
    assert vat_route.calls.last.request.url.params["code"] == "RC02"
    final_body = json.loads(compute_route.calls.last.request.content)
    assert final_body["rows"][0]["vat_code"]["code"] == "RC02"
    assert final_body["counterpart_version_id"] == 2001
    assert final_body["payment_method_id"] == 4001  # the only "Bonifico"
    assert final_body["add_tax_stamp_row"] is True
    assert data_params == final_body
    assert preview["total_amount"] == "1002.00"
    assert client["display_name"] == "ACME EUROPE SAS"


def test_ambiguous_payment_method_is_an_error(logged_in_client, api):
    mock_invoice_flow(api)
    api.get("/api/v1/payment_methods/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [{"id": 1, "method": "Bonifico"}, {"id": 2, "method": "Bonifico"}],
            },
        )
    )
    with pytest.raises(FiscozenError, match="payment_method_id"):
        logged_in_client.prepare_invoice(
            client_id=1001,
            invoice_date=date(2026, 10, 13),
            lines=[InvoiceLine("x", Decimal("1"))],
        )


def test_prepare_invoice_respects_customer_blocks(logged_in_client, api):
    mock_invoice_flow(api)
    api.get("/api/v1/invoices/customer_blocks/").mock(
        return_value=httpx.Response(
            200, json={"block_documents_creation": True, "message_create": {"title": "Blocked"}}
        )
    )
    with pytest.raises(FiscozenError, match="blocks invoice creation"):
        logged_in_client.prepare_invoice(
            client_id=1001,
            invoice_date=date(2026, 10, 13),
            lines=[InvoiceLine("x", Decimal("1"))],
        )


def test_create_invoice_posts_exactly_once(logged_in_client, api):
    route = api.post("/api/v1/invoices/").mock(return_value=httpx.Response(201, json=UNSENT))
    created = logged_in_client.create_invoice(
        load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
        {"total_amount": "1002.00"},
    )
    assert created["full_id"] == "1/2026"
    assert route.call_count == 1


def test_create_invoice_resent_once_after_403_relogin(logged_in_client, api):
    route = api.post("/api/v1/invoices/").mock(
        side_effect=[httpx.Response(403, json={}), httpx.Response(201, json=UNSENT)]
    )
    logged_in_client.create_invoice(
        load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
        {"total_amount": "1002.00"},
    )
    assert route.call_count == 2


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(500, text="boom"), httpx.ReadTimeout("slow"), httpx.ConnectError("x")],
)
def test_create_invoice_failure_is_never_retried(logged_in_client, api, failure):
    if isinstance(failure, httpx.Response):
        route = api.post("/api/v1/invoices/").mock(return_value=failure)
    else:
        route = api.post("/api/v1/invoices/").mock(side_effect=failure)
    with pytest.raises(FiscozenError, match="outcome unknown"):
        logged_in_client.create_invoice(
            load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
            {"total_amount": "1002.00"},
        )
    assert route.call_count == 1


def test_find_duplicates_on_real_invoice_list(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(return_value=httpx.Response(200, json=INVOICE_LIST))
    assert [
        d["full_id"] for d in logged_in_client.find_duplicates(1001, date(2026, 10, 13), "1002.00")
    ] == ["1/2026"]
    assert logged_in_client.find_duplicates(1001, date(2026, 10, 13), "100.00") == []
    assert logged_in_client.find_duplicates(999, date(2026, 10, 13), "1002.00") == []


def test_find_duplicates_fails_closed_on_unexpected_shape(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [{"id": 1, "invoice_date": "2026-10-13", "amount": None}],
            },
        )
    )
    with pytest.raises(FiscozenError, match="duplicates"):
        logged_in_client.find_duplicates(1001, date(2026, 10, 13), "1002.00")


def test_delete_refuses_delivered_invoice(logged_in_client, api):
    api.get("/api/v1/invoices/3001/").mock(return_value=httpx.Response(200, json=DELIVERED))
    delete_route = api.delete("/api/v1/invoices/3001/")
    with pytest.raises(FiscozenError, match="credit note"):
        logged_in_client.delete_invoice(3001)
    assert not delete_route.called


def test_delete_refuses_invoice_in_flight(logged_in_client, api):
    in_flight = {**UNSENT, "e_status": "SENDING"}
    assert "credit note" in logged_in_client.deletion_blocker(in_flight)


def test_delete_refuses_non_latest_invoice(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    {"invoice_number": 2, "invoice_date": "2026-11-01"},
                    {"invoice_number": 1, "invoice_date": "2026-10-13"},
                ],
            },
        )
    )
    assert "gap" in logged_in_client.deletion_blocker(UNSENT)


def test_delete_allows_latest_unsent_invoice(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(
            200,
            json={"next": None, "results": [{"invoice_number": 1, "invoice_date": "2026-10-13"}]},
        )
    )
    api.get("/api/v1/invoices/3001/").mock(return_value=httpx.Response(200, json=UNSENT))
    delete_route = api.delete("/api/v1/invoices/3001/").mock(return_value=httpx.Response(204))
    logged_in_client.delete_invoice(3001)
    assert delete_route.call_count == 1


# ---- config ------------------------------------------------------------------------------


def test_settings_from_env_and_file(tmp_path, monkeypatch):
    import fiscozen.config as config

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    (tmp_path / "config.toml").write_text('email = "file@example.com"\ndefault_client_id = 7\n')
    monkeypatch.delenv("FISCOZEN_EMAIL", raising=False)
    loaded = Settings.load()
    assert (loaded.email, loaded.default_client_id) == ("file@example.com", 7)
    monkeypatch.setenv("FISCOZEN_EMAIL", "env@example.com")
    assert Settings.load().email == "env@example.com"


def test_missing_password_explains_keychain_setup(monkeypatch):
    import subprocess

    monkeypatch.delenv("FISCOZEN_PASSWORD", raising=False)

    def keychain_miss(*args, **kwargs):
        raise subprocess.CalledProcessError(44, "security")

    monkeypatch.setattr(subprocess, "run", keychain_miss)
    with pytest.raises(ConfigError, match="add-generic-password"):
        Settings(email="me@example.com").password()


def test_session_save_failure_does_not_lose_a_created_invoice(logged_in_client, api, monkeypatch):
    def disk_full():
        raise OSError("disk full")

    monkeypatch.setattr(logged_in_client, "_save_session", disk_full)
    api.post("/api/v1/invoices/").mock(
        return_value=httpx.Response(
            201, json=UNSENT, headers={"set-cookie": "sessionid=rotated; Path=/"}
        )
    )
    created = logged_in_client.create_invoice(
        load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
        {"total_amount": "1002.00"},
    )
    assert created["full_id"] == "1/2026"


def test_fixture_sanitizer_masks_private_people_and_ibans():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_fixtures", Path(__file__).parents[2] / "tools" / "make_fixtures.py"
    )
    make_fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_fixtures)
    person = make_fixtures.sanitize(
        {
            "typology": "PRIV",
            "display_name": "Mario Rossi",
            "business_name": "Mario Rossi",
            "ssn": "RSSMRA80A01H501U",
            "note": "pay to IT60 X054 2811 1010 0000 0123 456",
        }
    )
    assert person["display_name"] == person["business_name"] == "Test Person"
    assert "RSSMRA" not in str(person) and "0123 456" not in str(person)
    located = make_fixtures.sanitize(
        {"typology": "PRIV", "municipality": "Bologna", "postcode": "40100", "website": "x.it"}
    )
    assert "Bologna" not in str(located) and "40100" not in str(located)
    paypal = make_fixtures.sanitize({"details_description": "PayPal", "details": "me@x.it"})
    sole_trader = make_fixtures.sanitize({"typology": "PROF", "vat": "01234567890"})
    assert sole_trader["vat"] == "00000000000"
    free_text = make_fixtures.sanitize({"description": "Consulenza per RSSMRA80A01H501U"})
    assert "RSSMRA" not in free_text["description"]
    assert paypal["details"] == "masked-details"
    company = make_fixtures.sanitize({"typology": "SOCI", "business_name": "ACME SRL"})
    assert company["business_name"] == "ACME SRL"


def test_session_of_another_account_is_not_reused(settings, api):
    settings.session_file.write_text(
        json.dumps({"email": "other@example.com", "sessionid": "theirs", "csrftoken": "x"})
    )
    mock_login(api)
    route = api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 1}))
    FiscozenClient(settings).user_id()
    assert "sessionid=session-id" in route.calls.last.request.headers["Cookie"]
    assert "theirs" not in route.calls.last.request.headers["Cookie"]


def test_deletion_blocker_with_empty_list_refuses(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": []})
    )
    assert "refusing" in logged_in_client.deletion_blocker(UNSENT)


def test_private_person_matched_by_tax_code(logged_in_client, api):
    api.get("/api/v1/counterparts/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [{"counterpart": 4, "vat": "", "ssn": "rssmra80a01h501u"}],
            },
        )
    )
    assert [
        c["counterpart"]
        for c in logged_in_client.find_existing_clients(tax_code="RSSMRA80A01H501U")
    ] == [4]
    assert logged_in_client.find_existing_clients() == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201),  # empty body
        httpx.Response(200, text="<html>Manutenzione in corso</html>"),
        httpx.Response(302, headers={"location": "/app/login"}),
        httpx.Response(201, json={"detail": "ok"}),  # no invoice id
        httpx.DecodingError("broken gzip"),
    ],
)
def test_create_odd_responses_are_outcome_unknown(logged_in_client, api, response):
    if isinstance(response, Exception):
        route = api.post("/api/v1/invoices/").mock(side_effect=response)
    else:
        route = api.post("/api/v1/invoices/").mock(return_value=response)
    with pytest.raises(CreateOutcomeUnknown):
        logged_in_client.create_invoice(
            load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
            {"total_amount": "1002.00"},
        )
    assert route.call_count == 1


def test_create_4xx_is_a_definite_rejection(logged_in_client, api):
    api.post("/api/v1/invoices/").mock(
        return_value=httpx.Response(400, json={"amount": ["invalid"]})
    )
    with pytest.raises(FiscozenError) as error:
        logged_in_client.create_invoice(
            load_fixture("invoice_created_with_stamp")["request"]["_data_params"],
            {"total_amount": "1002.00"},
        )
    assert not isinstance(error.value, CreateOutcomeUnknown)


def test_redirect_on_delete_is_not_success(logged_in_client, api):
    api.delete("/api/v1/counterparts/5/").mock(
        return_value=httpx.Response(302, headers={"location": "/app/login"})
    )
    with pytest.raises(FiscozenError):
        logged_in_client.delete_client(5)


def test_old_malformed_invoice_does_not_block_duplicate_check(logged_in_client, api):
    api.get("/api/v1/users/self/").mock(return_value=httpx.Response(200, json={"id": 5001}))
    api.get("/api/v1/invoices/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [{"id": 1, "invoice_date": "2024-01-10", "amount": None}],
            },
        )
    )
    assert logged_in_client.find_duplicates(1001, date(2026, 10, 13), "1002.00") == []


def test_existing_clients_matched_on_vat_or_tax_code(logged_in_client, api):
    api.get("/api/v1/counterparts/").mock(
        return_value=httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    {"counterpart": 4, "vat": "", "ssn": "RSSMRA80A01H501U"},
                    {"counterpart": 5, "vat": "12345678901", "ssn": ""},
                ],
            },
        )
    )
    found = logged_in_client.find_existing_clients(vat="99999999999", tax_code="RSSMRA80A01H501U")
    assert [c["counterpart"] for c in found] == [4]
    both = logged_in_client.find_existing_clients(vat="IT12345678901", tax_code="RSSMRA80A01H501U")
    assert [c["counterpart"] for c in both] == [4, 5]
