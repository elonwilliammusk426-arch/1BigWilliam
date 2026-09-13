from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class TelnyxClient:
    api_key: str
    base_url: str = 'https://api.telnyx.com/v2'

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError('TELNYX_API_KEY is required')
        self.base_url = self.base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })

    def get_message(self, message_id: str) -> dict[str, Any]:
        resp = self.session.get(f'{self.base_url}/messages/{message_id}', timeout=20)
        resp.raise_for_status()
        return resp.json()

    def list_messaging_detail_records(
        self,
        *,
        date_range: str | None = None,
        direction: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            'filter[record_type]': 'messaging',
            'page[size]': max(1, min(int(limit), 100)),
        }
        if date_range:
            params['filter[date_range]'] = date_range
        if direction:
            params['filter[direction]'] = direction
        resp = self.session.get(f'{self.base_url}/detail_records', params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get('data', [])

    def search_available_numbers(
        self,
        *,
        country_code: str = 'US',
        area_code: str | None = None,
        limit: int = 10,
        phone_number_type: str = 'local',
        features: str = 'sms',
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            'filter[country_code]': country_code.upper(),
            'filter[features]': features,
            'filter[limit]': max(1, min(int(limit), 50)),
            'filter[phone_number_type]': phone_number_type,
        }
        if area_code:
            params['filter[national_destination_code]'] = area_code
        resp = self.session.get(f'{self.base_url}/available_phone_numbers', params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get('data', [])
