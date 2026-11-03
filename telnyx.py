from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


MAX_DETAIL_RECORDS = 500
DETAIL_PAGE_SIZE = 100
MAX_DETAIL_PAGES = 10


class PaginationLimitError(Exception):
    """The provider has more pages than this bounded sync is allowed to fetch."""


class PartialDetailRecordsError(Exception):
    """A later page failed; preserve already fetched records for processing."""

    def __init__(self, records: list[dict[str, Any]], cause: Exception) -> None:
        super().__init__('Could not retrieve all requested detail-record pages')
        self.records = records
        self.cause = cause


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
        target = max(1, min(int(limit), MAX_DETAIL_RECORDS))
        page_size = min(target, DETAIL_PAGE_SIZE)
        params: dict[str, Any] = {
            'filter[record_type]': 'messaging',
            'page[size]': page_size,
            'sort': '-created_at',
        }
        if date_range:
            params['filter[date_range]'] = date_range
        if direction:
            params['filter[direction]'] = direction

        records: list[dict[str, Any]] = []
        for page_number in range(1, MAX_DETAIL_PAGES + 1):
            params['page[number]'] = page_number
            try:
                resp = self.session.get(f'{self.base_url}/detail_records', params=dict(params), timeout=20)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('Telnyx returned an invalid detail-record response')
            except Exception as exc:
                if records:
                    raise PartialDetailRecordsError(records, exc) from exc
                raise

            page = payload['data']
            records.extend(page[:target - len(records)])
            if not page or len(records) >= target:
                return records
            meta = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}
            try:
                total_pages = int(meta.get('total_pages'))
            except (TypeError, ValueError, OverflowError):
                total_pages = None
            if total_pages is not None:
                if page_number >= total_pages:
                    return records
            elif len(page) < page_size:
                return records

        # Never scan an unbounded number of pages, even with faulty metadata.
        raise PartialDetailRecordsError(records, PaginationLimitError())

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
