"""Telnyx SMS provider adapter.

This is the only outbound/provider file the rest of the toolkit needs to know
about. It wraps Telnyx's generic Messaging API so the CLI/OTP layers can send
using the simple model:

    send from YOUR Telnyx number -> recipient number -> text body

No public number-rental or third-party account-activation workflow is included.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class SendResult:
    """Normalized result returned by TelnyxClient.send_message()."""

    to_number: str
    from_number: str
    status: str
    message_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status.lower() not in {
            "failed",
            "delivery_failed",
            "rejected",
            "undeliverable",
        }


class TelnyxClient:
    """Small REST client for Telnyx Messaging API v2."""

    def __init__(self, api_key: str, base_url: str = "https://api.telnyx.com/v2"):
        if not api_key:
            raise ValueError("TELNYX_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def send_message(
        self,
        from_number: str,
        to_number: str,
        body: str,
        *,
        messaging_profile_id: str | None = None,
        webhook_url: str | None = None,
    ) -> SendResult:
        """Send an SMS from one of your Telnyx SMS-enabled numbers.

        Phone numbers should be E.164 formatted, e.g. +12015550123.
        """
        payload: dict[str, Any] = {
            "from": from_number,
            "to": to_number,
            "text": body,
        }
        if messaging_profile_id:
            payload["messaging_profile_id"] = messaging_profile_id
        if webhook_url:
            payload["webhook_url"] = webhook_url

        try:
            resp = self.session.post(
                f"{self.base_url}/messages", json=payload, timeout=20
            )
        except requests.RequestException as exc:
            return SendResult(
                to_number=to_number,
                from_number=from_number,
                status="request_error",
                error=str(exc),
            )

        try:
            data = resp.json()
        except ValueError:
            data = {"raw_text": resp.text}

        if not (200 <= resp.status_code < 300):
            return SendResult(
                to_number=to_number,
                from_number=from_number,
                status=f"http_{resp.status_code}",
                error=_extract_error(data),
                raw=data,
            )

        msg = data.get("data", data)
        message_id = msg.get("id")
        status = "queued"
        to_entries = msg.get("to")
        if isinstance(to_entries, list) and to_entries:
            status = to_entries[0].get("status") or status
        elif isinstance(to_entries, dict):
            status = to_entries.get("status") or status
        elif msg.get("record_type") == "message":
            status = "accepted"

        return SendResult(
            to_number=to_number,
            from_number=from_number,
            status=status,
            message_id=message_id,
            raw=data,
        )

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Fetch a message record by id."""
        resp = self.session.get(f"{self.base_url}/messages/{message_id}", timeout=20)
        resp.raise_for_status()
        return resp.json()

    def list_messaging_detail_records(
        self,
        *,
        date_range: str = "today",
        direction: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List Telnyx messaging detail records (MDRs)."""
        params: dict[str, Any] = {
            "filter[record_type]": "messaging",
            "filter[date_range]": date_range,
            "page[size]": max(1, min(int(limit), 100)),
        }
        if direction:
            params["filter[direction]"] = direction
        resp = self.session.get(f"{self.base_url}/detail_records", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def list_owned_numbers(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """List phone numbers already owned by this Telnyx account."""
        params = {"page[size]": max(1, min(int(page_size), 250))}
        resp = self.session.get(f"{self.base_url}/phone_numbers", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def search_available_numbers(
        self,
        *,
        country_code: str = "US",
        area_code: str | None = None,
        limit: int = 10,
        phone_number_type: str = "local",
        features: str = "sms",
        locality: str | None = None,
        administrative_area: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Telnyx inventory for SMS-capable numbers available to buy.

        Telnyx requires `filter[country_code]`. Common optional filters include
        `filter[national_destination_code]` for NANP area code, `filter[locality]`
        for city, `filter[administrative_area]` for state/province, and
        `filter[features]` such as sms.
        """
        limit = max(1, min(int(limit), 50))
        params: dict[str, Any] = {
            "filter[country_code]": country_code.upper(),
            "filter[features]": features,
            "filter[limit]": limit,
        }
        if phone_number_type:
            params["filter[phone_number_type]"] = phone_number_type
        if area_code:
            params["filter[national_destination_code]"] = area_code
        if locality:
            params["filter[locality]"] = locality
        if administrative_area:
            params["filter[administrative_area]"] = administrative_area

        resp = self.session.get(
            f"{self.base_url}/available_phone_numbers", params=params, timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])


def _extract_error(data: dict[str, Any]) -> str:
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first.get("detail") or first.get("title") or str(first)
        return str(first)
    return data.get("message") or data.get("error") or str(data)
