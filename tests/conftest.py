import json
from pathlib import Path

import httpx
import pytest
import respx

from fiscozen.client import BASE_URL, FiscozenClient
from fiscozen.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Sanitized request/response pair captured from the real web app."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("FISCOZEN_PASSWORD", "secret-password")
    return Settings(
        email="me@example.com", default_client_id=1001, session_file=tmp_path / "session.json"
    )


@pytest.fixture
def api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def mock_login(api) -> None:
    api.get("/app/").mock(
        return_value=httpx.Response(200, headers={"set-cookie": "csrftoken=pre-login-csrf; Path=/"})
    )
    api.post("/api-auth/browser/v1/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"status": 200},
            headers=[
                ("set-cookie", "csrftoken=session-csrf; Domain=.fiscozen.it; Path=/"),
                ("set-cookie", "sessionid=session-id; HttpOnly; Path=/"),
            ],
        )
    )


@pytest.fixture
def logged_in_client(settings, api) -> FiscozenClient:
    mock_login(api)
    client = FiscozenClient(settings)
    client.login()
    return client
