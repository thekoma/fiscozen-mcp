# Fiscozen private API map

Reverse-engineered from browser traffic recorded on 2026-10-02 (`tools/capture.py`).
Base URL `https://app.fiscozen.it`. Django REST Framework: lists are paginated
(`count`, `next`, `previous`, `results`), `OPTIONS` on a collection returns the field schema.

## Auth

| Step | Request | Result |
|---|---|---|
| 1 | `GET /app/` | sets `csrftoken` cookie |
| 2 | `POST /api-auth/browser/v1/auth/login` `{"email","password"}` + `X-CSRFToken` | sets `sessionid` (~14 days) and a rotated `csrftoken` (Domain `.fiscozen.it`) |

Unsafe methods need `X-CSRFToken: <csrftoken>`, plus `Origin`/`Referer` on app.fiscozen.it.
An unauthenticated call answers 403. No 2FA step was observed for email + password login.

## Invoices

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/invoices/compute_data/` | Preview: number (`full_id`), rows, totals, legal notes, `vat_code_choices_filters`. Saves nothing. |
| GET | `/api/v1/vat_codes/?code=&day=&document_kind=INVOICE&fiscal_regime=` | Full VAT code object for a row (e.g. RC02, reverse charge art. 7-ter) |
| GET | `/api/v1/invoices/customer_blocks/?counterpart_version_id=&ateco=&invoice_date=` | `block_documents_creation/edit/sending` flags |
| POST | `/api/v1/invoices/` | Header fields + `_data_params` (= compute_data body). 201, `status: DA_INCASSARE`, `sdi_document_status: null`. **Not idempotent.** |
| GET | `/api/v1/invoices/?self_invoice=false&user=<profile id>&ordering=...&page_size=` | List |
| GET/DELETE | `/api/v1/invoices/{id}/` | Delete only unsent invoices; deleting the latest frees its number |
| GET | `/api/v1/invoices/invoice_format_info/?day=` | e-invoicing mandatory since 2024 |

Creating does not transmit. SdI sending is a separate web-app action ("Invia fattura
elettronica", with its own confirmation modal); this project does not implement it.

The invoice links the client through `counterpart` + `counterpart_version`; the
`customer` snapshot field is `null` on invoices created by the web app too.

Legal notes are generated server-side: reverse charge art. 7-ter, "Imposta di bollo assolta
in modo virtuale". `add_tax_stamp_row: true` adds a 2.00 EUR "Marca da bollo" row charged to
the customer.

## Clients (counterparts)

Versioned: editing a client creates a new `counterpart_version`; invoices reference the
version. Always read the latest version right before invoicing.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/counterparts/?search=&counterpart_type=CUSTOMER` | List |
| GET/DELETE | `/api/v1/counterparts/{id}/` | `counterpart` = id, `counterpart_version` = version id |
| POST | `/api/v1/counterparts/` | Required: `typology` (SOCI, PROF, PRIV, PA, NOPR), `counterpart_type`, `address`, `municipality`, `postcode`, `country` |
| GET | `/api/v1/vat/check/?country=&vat=` | Company name and address from a VAT number. A tax code returns a 503 maintenance page |
| GET | `/api/v1/municipalities/autocomplete_postcode_plus/?n=&q=` | Italian postcode lookup |

## Other

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/users/self/` | Profile; its `id` is the `user` filter for lists (not the auth user id) |
| GET/POST | `/api/v1/payment_methods/` | `{"method","holder","details"}` |
| GET | `/api/v1/ateco_choices/?day=&user=` | Account's ATECO codes |
| GET/POST | `/api/v1/communications/` | Messages with the accountant (POST is multipart `message`) |
| POST/DELETE | `/api/v1/receipts/`, `/api/v1/passive_invoices/` | Seen, not implemented |
| POST | `/api/v1/credit_notes/` | Requires the original `invoice`; not implemented |
