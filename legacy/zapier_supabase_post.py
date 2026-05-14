"""ORIGINAL Zapier "Run Python" code — kept verbatim for reference.

This is the existing pipeline being replaced. Do NOT import or run from the new
service code. The new Supabase writer (`src/autoreplies/services/supabase.py`)
preserves the field-mapping and merge-duplicates semantics; coercion helpers
have been ported into `src/autoreplies/utils/coerce.py`.

Notes for the migration:
- `input_data` was provided by Zapier from earlier steps in the Zap.
- The new pipeline assembles the same field shape but the values come from the
  Gmail / Airtable lookups instead.
- Per Sam: rental-platform leads via this new pipeline always get sales=False
  and use the Airtable record ID as the Supabase primary key (`id`).
"""

import json
import re
import requests

SUPABASE_URL = "https://fuacxndojzybijrqdbym.supabase.co/rest/v1/inquiries"
SUPABASE_KEY = input_data["supabase_key"]


def to_null(value):
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else value


def to_number_or_null(value):
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    s = re.sub(r"[$,]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def to_date_or_null(value):
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else s


payload = {
    "id": to_null(input_data.get("id")),
    "apartment": to_null(input_data.get("apartment")),
    "apartment_id": to_null(input_data.get("apartment_id")),
    "date_created": to_date_or_null(input_data.get("date_created")),
    "name_form": to_null(input_data.get("name_form")),
    "email_form": to_null(input_data.get("email_form")),
    "phone": to_null(input_data.get("phone")),
    "credit": to_number_or_null(input_data.get("credit")),
    "income": to_number_or_null(input_data.get("income")),
    "guarantor": to_null(input_data.get("guarantor")),
    "apartment_failsafe": to_null(input_data.get("apartment_failsafe")),
    "message": to_null(input_data.get("message")),
    "budget": to_number_or_null(input_data.get("budget")),
    "move_in_date": to_date_or_null(input_data.get("move_in_date")),
    "user_id": to_null(input_data.get("user_id")),
    "user": to_null(input_data.get("user")),
    "email": to_null(input_data.get("email")),
    "name": to_null(input_data.get("name")),
    "type_platform": to_null(input_data.get("type_platform")),
    "apartment_active_date": to_null(input_data.get("apartment_active_date")),
    "method": to_null(input_data.get("method")),
    "sales": to_null(input_data.get("sales")),
}

# Remove null fields so blank Airtable values do not break numeric/date columns
payload = {k: v for k, v in payload.items() if v is not None}

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=representation",
}

response = requests.post(
    SUPABASE_URL,
    headers=headers,
    data=json.dumps(payload),
    timeout=30,
)

if not response.ok:
    raise Exception(f"Supabase error {response.status_code}: {response.text}")

try:
    body = response.json()
except Exception:
    body = response.text

output = {
    "status_code": response.status_code,
    "response": body,
    "sent_payload": payload,
}
