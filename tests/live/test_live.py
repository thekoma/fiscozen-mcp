"""Read-only checks against the real account. Run with: FISCOZEN_LIVE=1 uv run pytest -m live

Nothing here creates, edits or deletes: prepare_invoice only calls the preview endpoint.
"""

import os
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from fiscozen.client import FiscozenClient
from fiscozen.config import Settings
from fiscozen.invoice import InvoiceLine

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("FISCOZEN_LIVE") != "1", reason="FISCOZEN_LIVE=1 not set"),
]


@pytest.fixture(scope="module")
def client():
    fiscozen = FiscozenClient(Settings.load())
    yield fiscozen
    fiscozen.close()


def test_login_and_profile(client):
    assert client.user_id() > 0


def test_default_client_exists(client):
    assert client.settings.default_client_id, "set default_client_id in the config"
    assert client.get_client(client.settings.default_client_id)["counterpart_version"]


def test_preview_does_not_create(client):
    before = len(client.list_invoices())
    _, preview, _ = client.prepare_invoice(
        client_id=client.settings.default_client_id,
        lines=[InvoiceLine("Live preview check", Decimal("1.00"))],
        invoice_date=datetime.now(ZoneInfo("Europe/Rome")).date(),
    )
    assert preview["full_id"]
    assert len(client.list_invoices()) == before
