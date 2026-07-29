"""Optional Numverify phone-number lookup helper.

Numverify validates a phone number and returns metadata such as country,
carrier, and line type. It does not buy numbers, receive SMS, or replace
Telnyx. It is only a lookup/check tool used by the /checknum bot command.
"""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_NUMVERIFY_BASE_URL = "https://apilayer.net/api/validate"


class NumverifyError(RuntimeError):
    """Raised when Numverify cannot validate/check a number."""


def validate_number(
    number: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    country_code: str | None = None,
) -> dict[str, Any]:
    """Validate a phone number through Numverify.

    Required env var:
        NUMVERIFY_API_KEY

    Optional env var:
        NUMVERIFY_BASE_URL, default https://apilayer.net/api/validate
    """
    key = api_key or os.getenv("NUMVERIFY_API_KEY", "").strip()
    if not key:
        raise NumverifyError("Missing NUMVERIFY_API_KEY")

    url = (base_url or os.getenv("NUMVERIFY_BASE_URL", DEFAULT_NUMVERIFY_BASE_URL)).strip()
    params: dict[str, Any] = {
        "access_key": key,
        "number": number,
        "format": 1,
    }
    if country_code:
        params["country_code"] = country_code.upper()

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise NumverifyError(f"Numverify request failed: {exc}") from exc
    except ValueError as exc:
        raise NumverifyError("Numverify returned non-JSON response") from exc

    if data.get("success") is False and data.get("error"):
        err = data.get("error") or {}
        info = err.get("info") or err.get("type") or str(err)
        raise NumverifyError(f"Numverify error: {info}")

    return data


def format_numverify_result(data: dict[str, Any]) -> str:
    """Format Numverify JSON for Telegram."""
    valid = data.get("valid")
    valid_text = "Yes" if valid is True else "No" if valid is False else "Unknown"

    fields = [
        "📞 Number check",
        f"Valid: {valid_text}",
        f"Number: {data.get('number') or '-'}",
        f"Local format: {data.get('local_format') or '-'}",
        f"International: {data.get('international_format') or '-'}",
        f"Country: {data.get('country_name') or '-'} ({data.get('country_code') or '-'})",
        f"Location: {data.get('location') or '-'}",
        f"Carrier: {data.get('carrier') or '-'}",
        f"Line type: {data.get('line_type') or '-'}",
    ]
    return "\n".join(fields)
