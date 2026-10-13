"""HTTP client for the Fiscozen web app's private REST API (app.fiscozen.it).

Auth is the Django browser flow: GET /app/ hands out a `csrftoken` cookie, then
POST /api-auth/browser/v1/auth/login returns `sessionid` (valid ~14 days) plus a rotated
`csrftoken`. Unsafe requests must echo the csrftoken in `X-CSRFToken`.

Nothing in here can send an invoice to SdI: there is deliberately no method for it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .invoice import (
    InvoiceLine,
    build_create_body,
    build_data_params,
    build_rows,
    money,
)

BASE_URL = "https://app.fiscozen.it"
BASE_HOST = urlsplit(BASE_URL).hostname
SESSION_COOKIES = ("sessionid", "csrftoken")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# e_status of an e-invoice that was never handed to SdI; anything else is sent or in flight
UNSENT_E_STATUS = "TO_SEND"
# Same client + same total within this many days counts as a possible double submit
DUPLICATE_WINDOW_DAYS = 7


class FiscozenError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class CreateOutcomeUnknown(FiscozenError):
    """The create request may or may not have been committed (timeout, dropped link, 5xx)."""


class FiscozenClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/app/",
            },
        )
        # MCP runs sync tools on worker threads: one lock serializes session use and login.
        # Re-entrant because request() may call login() while holding it.
        self.lock = threading.RLock()
        # Cookies are tracked by hand: the server sets csrftoken both host-only and for
        # .fiscozen.it, and a cookie jar would then send two conflicting values.
        self._cookies: dict[str, str] = self._load_session()
        self._user_id: int | None = None

    def close(self) -> None:
        self._http.close()

    # ---- session handling -------------------------------------------------------------

    def _load_session(self) -> dict[str, str]:
        path: Path = self.settings.session_file
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("email") != self.settings.email:
            return {}  # a session of another account must never be reused
        return {k: v for k, v in data.items() if k in SESSION_COOKIES and isinstance(v, str)}

    def _save_session(self) -> None:
        """Atomic write of a 0600 file, even if an older session file had looser permissions."""
        path: Path = self.settings.session_file
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".session-")  # created 0600
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"email": self.settings.email, **self._cookies}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One raw request with our cookies; captures any rotated session cookies."""
        target = urlsplit(url)
        # Session cookies are attached by hand, so never let them leave https://app.fiscozen.it
        is_absolute = bool(target.scheme or target.netloc)
        if is_absolute and (target.scheme != "https" or target.hostname != BASE_HOST):
            raise FiscozenError(f"Refusing to send session cookies to {url}")
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        if method in UNSAFE_METHODS and "csrftoken" in self._cookies:
            headers["X-CSRFToken"] = self._cookies["csrftoken"]
        response = self._http.request(method, url, headers=headers, **kwargs)

        changed = False
        for cookie in response.cookies.jar:
            if cookie.name in SESSION_COOKIES and self._cookies.get(cookie.name) != cookie.value:
                self._cookies[cookie.name] = cookie.value
                changed = True
        self._http.cookies.clear()  # keep httpx's own jar out of the picture
        if changed and "sessionid" in self._cookies:
            # Best effort: never lose a response (e.g. a 201) over a cache write
            with contextlib.suppress(OSError):
                self._save_session()
        return response

    def login(self) -> None:
        with self.lock:
            self._cookies = {}
            self._send("GET", "/app/")  # obtains the pre-login csrftoken
            if "csrftoken" not in self._cookies:
                raise FiscozenError("Login failed: no CSRF cookie from /app/")
            response = self._send(
                "POST",
                "/api-auth/browser/v1/auth/login",
                json={"email": self.settings.email, "password": self.settings.password()},
                headers={"Referer": f"{BASE_URL}/app/login"},
            )
            if response.status_code != 200 or "sessionid" not in self._cookies:
                # Never echo the request body: it contains the password
                raise FiscozenError(
                    f"Login failed (HTTP {response.status_code})",
                    status=response.status_code,
                    detail=_safe_json(response),
                )
            with contextlib.suppress(OSError):  # a cache write must not fail a good login
                self._save_session()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """JSON request; logs in on demand and retries exactly once on 401/403.

        A 401/403 means Django rejected the request before running the view, so resending
        a POST after re-login cannot create a duplicate. Nothing else is ever retried.
        """
        with self.lock:
            if "sessionid" not in self._cookies:
                self.login()
            response = self._send(method, url, **kwargs)
            if response.status_code in (401, 403):
                self.login()
                response = self._send(method, url, **kwargs)
        if response.status_code >= 300:  # httpx does not follow redirects: never success
            detail = _safe_json(response)
            raise FiscozenError(
                f"{method} {url} failed (HTTP {response.status_code}): {detail}",
                status=response.status_code,
                detail=detail,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _paginate(self, url: str, params: dict) -> list[dict]:
        """Follow DRF `next` links, keeping only their path and query (never their host)."""
        results: list[dict] = []
        page = self.request("GET", url, params=params)
        while True:
            if isinstance(page, list):  # some endpoints skip pagination entirely
                return results + page
            results.extend(page.get("results", []))
            if not page.get("next"):
                return results
            next_link = urlsplit(page["next"])
            page = self.request("GET", f"{next_link.path}?{next_link.query}")

    # ---- account ----------------------------------------------------------------------

    def user_id(self) -> int:
        """Fiscozen profile id (differs from the auth user id returned by login)."""
        if self._user_id is None:
            self._user_id = self.request("GET", "/api/v1/users/self/")["id"]
        return self._user_id

    def payment_methods(self) -> list[dict]:
        return self._paginate("/api/v1/payment_methods/", {"page_size": 100})

    def ateco_codes(self, day: date) -> list[str]:
        choices = self.request(
            "GET", "/api/v1/ateco_choices/", params={"day": day.isoformat(), "user": self.user_id()}
        )
        return [choice["value"] for choice in choices]

    # ---- clients (counterparts) ---------------------------------------------------------

    def list_clients(self, search: str = "") -> list[dict]:
        return self._paginate(
            "/api/v1/counterparts/",
            {"search": search, "counterpart_type": "CUSTOMER", "ordering": "display_name"},
        )

    def find_existing_clients(
        self, vat: str = "", tax_code: str = "", name: str = "", country: str = ""
    ) -> list[dict]:
        """Clients with the same VAT number (prefix-insensitive) and/or tax code; without
        either, clients with the same name in the same country."""
        wanted_vat = _normalize_vat(vat) if vat else None
        wanted_code = tax_code.strip().upper() if tax_code else None
        wanted_name = _normalize_name(name) if not (vat or tax_code) and name else None
        if not (wanted_vat or wanted_code or wanted_name):
            return []
        # Full list: the search filter is not known to match on VAT numbers or tax codes
        return [
            c
            for c in self.list_clients()
            if (wanted_vat and _normalize_vat(c.get("vat") or "") == wanted_vat)
            or (wanted_code and (c.get("ssn") or "").strip().upper() == wanted_code)
            or (
                wanted_name
                and _normalize_name(c.get("business_name") or c.get("display_name") or "")
                == wanted_name
                and (c.get("country") or "").upper() == country.upper()
            )
        ]

    def get_client(self, client_id: int) -> dict:
        return self.request("GET", f"/api/v1/counterparts/{client_id}/")

    def check_vat(self, country: str, vat: str) -> dict:
        """Company data from a VAT number (works for VAT numbers, not tax codes)."""
        return self.request(
            "GET", "/api/v1/vat/check/", params={"country": country.upper(), "vat": vat}
        )

    def create_client(self, body: dict) -> dict:
        """body from invoice.build_client_body()."""
        return self.request("POST", "/api/v1/counterparts/", json=body)

    def delete_client(self, client_id: int) -> None:
        self.request("DELETE", f"/api/v1/counterparts/{client_id}/")

    # ---- invoices -----------------------------------------------------------------------

    def list_invoices(self, year: int | None = None) -> list[dict]:
        invoices = self._paginate(
            "/api/v1/invoices/",
            {
                "self_invoice": "false",
                "user": self.user_id(),
                "ordering": "-invoice_date,-invoice_number,-id",
                "page_size": 50,
            },
        )
        if year is not None:
            invoices = [i for i in invoices if str(i.get("invoice_date", "")).startswith(str(year))]
        return invoices

    def get_invoice(self, invoice_id: int) -> dict:
        return self.request("GET", f"/api/v1/invoices/{invoice_id}/")

    def deletion_blocker(self, invoice: dict) -> str | None:
        """Why this invoice must not be deleted, or None if deleting it is safe."""
        sent = (
            invoice.get("e_status") != UNSENT_E_STATUS
            or invoice.get("e_invoice_sent_date")
            or invoice.get("sdi_document_status") is not None
            or invoice.get("sdi_document_sent_at")
        )
        if sent:
            return (
                f"Invoice {invoice.get('full_id')} is sent or being sent to SdI "
                f"(e_status={invoice.get('e_status')}): it needs a credit note, not a deletion"
            )
        year = int(str(invoice["invoice_date"])[:4])
        numbers = [int(i["invoice_number"]) for i in self.list_invoices(year)]
        if not numbers:
            return f"Invoice {invoice.get('full_id')} not found in the {year} list; refusing"
        latest = max(numbers)
        if int(invoice["invoice_number"]) < latest:
            return (
                f"Invoice {invoice.get('full_id')} is not the latest of {year} "
                f"(latest number: {latest}): deleting it would leave a gap in the numbering"
            )
        return None

    def delete_invoice(self, invoice_id: int) -> None:
        with self.lock:
            invoice = self.get_invoice(invoice_id)
            if blocker := self.deletion_blocker(invoice):
                raise FiscozenError(blocker)
            self.request("DELETE", f"/api/v1/invoices/{invoice_id}/")

    def compute(self, data_params: dict) -> dict:
        """Server-side preview: numbering, totals, legal notes. Saves nothing."""
        return self.request("POST", "/api/v1/invoices/compute_data/", json=data_params)

    def _resolve_payment_method(self, explicit: int | None) -> int:
        if explicit is not None:
            return explicit
        if self.settings.default_payment_method_id is not None:
            return self.settings.default_payment_method_id
        transfers = [m for m in self.payment_methods() if m.get("method") == "Bonifico"]
        if len(transfers) != 1:
            raise FiscozenError(
                f"Found {len(transfers)} bank-transfer payment methods: pass payment_method_id "
                "or set default_payment_method_id in the config"
            )
        return transfers[0]["id"]

    def _resolve_ateco(self, day: date) -> str:
        if self.settings.ateco:
            return self.settings.ateco
        codes = self.ateco_codes(day)
        if len(codes) != 1:
            raise FiscozenError(f"Ambiguous ATECO codes {codes}: set `ateco` in the config")
        return codes[0]

    def _vat_code(self, filters: dict) -> dict:
        codes = self.request("GET", "/api/v1/vat_codes/", params=filters)
        if len(codes) != 1:
            raise FiscozenError(f"Expected exactly one VAT code for {filters}, got {len(codes)}")
        return codes[0]

    def prepare_invoice(
        self,
        *,
        client_id: int,
        lines: list[InvoiceLine],
        invoice_date: date,
        charge_stamp_duty: bool = True,
        payment_due_date: date | None = None,
        notes: list[str] | None = None,
        payment_method_id: int | None = None,
    ) -> tuple[dict, dict, dict]:
        """Build data params the way the web app does and preview them.

        Returns (data_params, preview, client). The VAT code is never guessed: an empty
        preview tells us which code applies to this client (e.g. RC02 for EU B2B).
        """
        if not lines:
            raise ValueError("An invoice needs at least one line")
        client = self.get_client(client_id)
        if not client.get("is_last_version", True):
            raise FiscozenError(f"Client {client_id} did not return its latest version")
        version_id = client["counterpart_version"]
        ateco = self._resolve_ateco(invoice_date)

        blocks = self.request(
            "GET",
            "/api/v1/invoices/customer_blocks/",
            params={
                "counterpart_version_id": version_id,
                "ateco": ateco,
                "invoice_date": invoice_date.isoformat(),
            },
        )
        if blocks.get("block_documents_creation"):
            message = blocks.get("message_create") or {}
            raise FiscozenError(f"Fiscozen blocks invoice creation: {message}")

        common = dict(
            counterpart_version_id=version_id,
            invoice_date=invoice_date,
            vat_ateco=ateco,
            payment_method_id=self._resolve_payment_method(payment_method_id),
            charge_stamp_duty=charge_stamp_duty,
            payment_due_date=payment_due_date,
            notes=notes,
        )
        empty_preview = self.compute(build_data_params(rows=[], **common))
        vat_code = self._vat_code(empty_preview["vat_code_choices_filters"])

        data_params = build_data_params(rows=build_rows(lines, vat_code), **common)
        preview = self.compute(data_params)
        if not preview.get("is_valid_invoice_date", True):
            raise FiscozenError(f"Fiscozen rejects invoice date {invoice_date.isoformat()}")
        return data_params, preview, client

    def find_duplicates(self, client_id: int, invoice_date: date, total: str) -> list[dict]:
        """Invoices for the same client and total dated within DUPLICATE_WINDOW_DAYS.

        A window, not the exact date: a retry after midnight gets a new default date.
        Fails closed: an invoice missing the fields we compare raises instead of being
        silently treated as different.
        """
        duplicates = []
        for invoice in self.list_invoices():
            try:
                days_apart = abs((date.fromisoformat(invoice["invoice_date"]) - invoice_date).days)
                if days_apart > DUPLICATE_WINDOW_DAYS:
                    continue  # old malformed invoices must not block new ones
                same_total = money(invoice["amount"]) == money(total)
                same_client = invoice["counterpart_id"] == client_id
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise FiscozenError(
                    f"Cannot check duplicates: unexpected invoice list item {invoice.get('id')}"
                ) from exc
            if same_total and same_client:
                duplicates.append(invoice)
        return duplicates

    def create_invoice(self, data_params: dict, preview: dict) -> dict:
        """Save the invoice. It stays unsent: transmission to SdI is done in the web app.

        Not idempotent: each call consumes a new invoice number. No automatic retries.
        """
        unknown = (
            "Invoice creation outcome unknown ({}): the invoice may exist. "
            "Check list_invoices before trying again."
        )
        try:
            created = self.request(
                "POST", "/api/v1/invoices/", json=build_create_body(data_params, preview)
            )
        except httpx.RequestError as exc:  # timeouts, dropped links, undecodable bodies
            raise CreateOutcomeUnknown(unknown.format(type(exc).__name__)) from exc
        except ValueError as exc:  # 2xx with a non-JSON body (e.g. an HTML maintenance page)
            raise CreateOutcomeUnknown(unknown.format("non-JSON response")) from exc
        except FiscozenError as exc:
            # Only a 4xx is a definite rejection; 3xx and 5xx may hide a committed invoice
            if exc.status is None or not 400 <= exc.status < 500:
                raise CreateOutcomeUnknown(
                    unknown.format(f"HTTP {exc.status}"), exc.status
                ) from exc
            raise
        if not isinstance(created, dict) or not created.get("id"):
            raise CreateOutcomeUnknown(unknown.format("response without an invoice id"))
        return created


def _normalize_vat(vat: str) -> str:
    """'IT 10914660153' and '10914660153' compare equal (prefix dropped on both sides)."""
    cleaned = "".join(ch for ch in vat.upper() if ch.isalnum())
    has_prefix = len(cleaned) > 2 and cleaned[:2].isalpha() and cleaned[2:3].isdigit()
    return cleaned[2:] if has_prefix else cleaned


def _normalize_name(name: str) -> str:
    return " ".join("".join(ch for ch in name.upper() if ch.isalnum() or ch.isspace()).split())


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:300]
