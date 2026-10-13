"""Turn raw captures into sanitized JSON fixtures for the offline tests.

Usage:
  uv run tools/make_fixtures.py har captures/a.har [captures/b.har ...]
  uv run tools/make_fixtures.py json <fixture-name> <response.json> [<request.json>]

Personal data is masked or stripped (seller profile, names, addresses, phone, tax codes,
IBAN, holder), so the output in tests/fixtures/ is safe to commit. Raw captures are not.

Business data (client names, amounts, Fiscozen ids) is replaced through an optional map in
captures/anonymize.json, git-ignored because its keys are the real values:

  {"strings": {"REAL CLIENT SRL": "ACME EUROPE SAS", "1234.56": "1000.00"},
   "ints": {"9999999": 1001}}

Strings are replaced as substrings (longest first); ints by exact value and inside strings.
"""

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"
# Optional real -> fake map for business data (client, amounts, ids). It stays in the
# git-ignored captures/ dir because its keys ARE the real values.
ANON_FILE = ROOT / "captures" / "anonymize.json"
ANON = json.loads(ANON_FILE.read_text()) if ANON_FILE.exists() else {}
ANON_STRINGS = sorted(ANON.get("strings", {}).items(), key=lambda kv: -len(kv[0]))
ANON_INTS = {int(k): v for k, v in ANON.get("ints", {}).items()}

# Keys removed anywhere in a payload: the seller's own profile and personal data
DROP_KEYS = {"user", "old_data"}
DROP_PREFIXES = ("business_", "birth_")  # seller address/birth data...
KEEP_KEYS = {"business_name"}  # ...but a client's company name is public and needed
# Keys kept for shape but with a placeholder value (only when the real value is non-empty)
MASK_KEYS = {
    "holder": "Test Holder",
    "email": "billing@example.com",
    "pec": "pec@example.com",
    "contact": "Test Contact",
    "address": "1 Test Street",
    "phone": "0000000000",
    "ssn": "XXXXXX00X00X000X",
    "first_name": "Test",
    "last_name": "Person",
}
IBAN = re.compile(r"\b[A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]){11,30}\b")
FAKE_IBAN = "IT00X0000000000000000000000"
# Italian tax code (codice fiscale) of a person, anywhere in free text
TAX_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
PERSON_NAME_KEYS = ("display_name", "counterpart_version_display_name", "business_name")
# For a person, location and contact details are personal data too
PERSON_DETAIL_MASKS = {
    "municipality": "Testville",
    "postcode": "00000",
    "website": "https://example.com",
    "fax": "0000000000",
    "vat": "00000000000",  # a sole trader's VAT number identifies them
    "recipient_code": "0000000",
}

# (fixture name, HTTP method, URL path, match mode, which: first/last)
# match mode: "exact" = URL path ends there (collection POSTs), "regex" on the path,
# "contains" otherwise
WANTED = [
    ("compute_empty", "POST", "/invoices/compute_data/", "contains", "first"),
    ("compute_with_row", "POST", "/invoices/compute_data/", "contains", "last"),
    ("vat_codes", "GET", "/api/v1/vat_codes/", "contains", "first"),
    ("customer_blocks", "GET", "/invoices/customer_blocks/", "contains", "first"),
    ("counterpart", "GET", r"/api/v1/counterparts/\d+/$", "regex", "first"),
    ("ateco_choices", "GET", "/api/v1/ateco_choices/", "contains", "first"),
    ("invoice_created", "POST", "/api/v1/invoices/", "exact", "first"),
    ("counterpart_created", "POST", "/api/v1/counterparts/", "exact", "first"),
]


def sanitize(value):
    if isinstance(value, dict):
        value = dict(value)
        # A private-person client's "company" name is a person's name
        person_types = {"PRIV", "PROF"}  # PROF: a professional's business name is their name
        is_person = (
            value.get("typology") in person_types
            or value.get("counterpart_typology") in person_types
            or value.get("first_name")
        )
        if is_person:
            for key in PERSON_NAME_KEYS:
                if value.get(key):
                    value[key] = "Test Person"
            for key, placeholder in PERSON_DETAIL_MASKS.items():
                if value.get(key):
                    value[key] = placeholder
        # Payment details are an IBAN, a PayPal email, ...: always masked
        if value.get("details"):
            value["details"] = (
                FAKE_IBAN if value.get("details_description") == "IBAN" else "masked-details"
            )
        return {
            k: MASK_KEYS[k] if k in MASK_KEYS and v else sanitize(v)
            for k, v in value.items()
            if k in KEEP_KEYS or (k not in DROP_KEYS and not k.startswith(DROP_PREFIXES))
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return ANON_INTS.get(value, value)
    if isinstance(value, str):
        for real, fake in ANON_STRINGS:
            value = value.replace(real, fake)
        for real_id, fake_id in ANON_INTS.items():
            value = value.replace(str(real_id), str(fake_id))
        return TAX_CODE.sub("XXXXXX00X00X000X", IBAN.sub(FAKE_IBAN, value))
    return value


def write_fixture(name: str, response, request=None) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixture = {"response": sanitize(response)}
    if request is not None:
        fixture["request"] = sanitize(request)
    (FIXTURE_DIR / f"{name}.json").write_text(json.dumps(fixture, indent=1, ensure_ascii=False))
    print(f"wrote {name}.json")


def body(entry, side):
    content = (
        entry["request"].get("postData", {}) if side == "request" else entry["response"]["content"]
    )
    text = content.get("text", "")
    if content.get("encoding") == "base64":
        text = base64.b64decode(text).decode()
    return json.loads(text)


def from_hars(har_paths: list[str]) -> None:
    entries = [e for p in har_paths for e in json.loads(Path(p).read_text())["log"]["entries"]]
    for name, method, path, mode, which in WANTED:
        url_path = lambda e: e["request"]["url"].split("?")[0]  # noqa: E731
        matches = [
            e
            for e in entries
            if e["request"]["method"] == method
            and (
                url_path(e).endswith(path)
                if mode == "exact"
                else re.search(path, url_path(e))
                if mode == "regex"
                else path in e["request"]["url"]
            )
        ]
        if not matches:
            print(f"skip {name}: not in captures")
            continue
        entry = matches[0] if which == "first" else matches[-1]
        request = body(entry, "request") if method == "POST" else None
        write_fixture(name, body(entry, "response"), request)


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "har":
        from_hars(args)
    elif mode == "json":
        name, response_path, *request_path = args
        request = json.loads(Path(request_path[0]).read_text()) if request_path else None
        write_fixture(name, json.loads(Path(response_path).read_text()), request)
    else:
        sys.exit(__doc__)
