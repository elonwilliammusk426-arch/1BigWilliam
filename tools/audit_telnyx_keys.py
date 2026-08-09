"""Audit many Telnyx API keys and separate keys with numbers.

Usage:
    python tools/audit_telnyx_keys.py telnyx_keys_input.txt

Input:
    One API key per line, or pasted text containing API keys.

Outputs (created in current directory):
    telnyx_valid_with_numbers.csv
    telnyx_valid_no_numbers.csv
    telnyx_invalid_keys.csv
    telnyx_audit_summary.txt
    railway_values_generated.env

Notes:
    - This script does not push secrets to GitHub.
    - Use only for Telnyx accounts you own/administer or are authorized to audit.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.telnyx.com/v2"
KEY_RE = re.compile(r"KEY[0-9A-Za-z_\-]{20,}")
WEBHOOK_URL = "https://1bigwilliam.up.railway.app/inbound/sms"


def mask(key: str) -> str:
    if len(key) <= 18:
        return key[:6] + "..."
    return f"{key[:12]}...{key[-6:]}"


def extract_keys(text: str) -> list[str]:
    found: list[str] = []
    for m in KEY_RE.finditer(text):
        key = m.group(0).strip().strip(",;()[]{}<>`'\"")
        if key and key not in found:
            found.append(key)
    return found


def get_json(url: str, key: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    return r.status_code, data


def first_error(data: dict[str, Any]) -> str:
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        e = errors[0]
        if isinstance(e, dict):
            return e.get("detail") or e.get("title") or str(e)
        return str(e)
    return data.get("message") or data.get("error") or str(data)[:300]


def audit_key(index: int, key: str) -> dict[str, Any]:
    status, data = get_json(f"{BASE_URL}/phone_numbers", key, {"page[size]": 100})
    result: dict[str, Any] = {
        "index": index,
        "api_key": key,
        "api_key_masked": mask(key),
        "status_code": status,
        "ok": False,
        "error": "",
        "numbers": [],
        "profiles": [],
    }
    if status != 200:
        result["error"] = first_error(data)
        return result

    result["ok"] = True
    numbers = []
    for n in data.get("data", []) or []:
        numbers.append(
            {
                "phone_number": n.get("phone_number", ""),
                "status": n.get("status", ""),
                "messaging_profile_name": n.get("messaging_profile_name", ""),
                "messaging_profile_id": n.get("messaging_profile_id", ""),
                "country": n.get("country_iso_alpha2", ""),
                "type": n.get("phone_number_type", ""),
            }
        )
    result["numbers"] = numbers

    p_status, p_data = get_json(f"{BASE_URL}/messaging_profiles", key, {"page[size]": 100})
    if p_status == 200:
        profiles = []
        for p in p_data.get("data", []) or []:
            profiles.append(
                {
                    "name": p.get("name", ""),
                    "id": p.get("id", ""),
                    "enabled": p.get("enabled", ""),
                    "api_version": p.get("webhook_api_version", ""),
                    "webhook_url": p.get("webhook_url", ""),
                    "webhook_ok": p.get("webhook_url") == WEBHOOK_URL,
                }
            )
        result["profiles"] = profiles
    else:
        result["profiles_error"] = first_error(p_data)

    return result


def write_outputs(results: list[dict[str, Any]]) -> None:
    with_numbers = [r for r in results if r["ok"] and r["numbers"]]
    no_numbers = [r for r in results if r["ok"] and not r["numbers"]]
    invalid = [r for r in results if not r["ok"]]

    with open("telnyx_valid_with_numbers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "index",
                "api_key",
                "api_key_masked",
                "phone_number",
                "number_status",
                "profile_name",
                "profile_id",
                "country",
                "number_type",
                "webhook_url",
                "webhook_ok",
                "api_version",
            ],
        )
        w.writeheader()
        for r in with_numbers:
            profile_by_id = {p.get("id"): p for p in r.get("profiles", [])}
            for n in r["numbers"]:
                p = profile_by_id.get(n.get("messaging_profile_id"), {})
                w.writerow(
                    {
                        "index": r["index"],
                        "api_key": r["api_key"],
                        "api_key_masked": r["api_key_masked"],
                        "phone_number": n.get("phone_number"),
                        "number_status": n.get("status"),
                        "profile_name": n.get("messaging_profile_name"),
                        "profile_id": n.get("messaging_profile_id"),
                        "country": n.get("country"),
                        "number_type": n.get("type"),
                        "webhook_url": p.get("webhook_url", ""),
                        "webhook_ok": p.get("webhook_ok", ""),
                        "api_version": p.get("api_version", ""),
                    }
                )

    with open("telnyx_valid_no_numbers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["index", "api_key", "api_key_masked"])
        w.writeheader()
        for r in no_numbers:
            w.writerow({"index": r["index"], "api_key": r["api_key"], "api_key_masked": r["api_key_masked"]})

    with open("telnyx_invalid_keys.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["index", "api_key", "api_key_masked", "status_code", "error"])
        w.writeheader()
        for r in invalid:
            w.writerow(
                {
                    "index": r["index"],
                    "api_key": r["api_key"],
                    "api_key_masked": r["api_key_masked"],
                    "status_code": r["status_code"],
                    "error": r["error"],
                }
            )

    api_keys = [r["api_key"] for r in with_numbers]
    phone_numbers: list[str] = []
    for r in with_numbers:
        for n in r["numbers"]:
            num = n.get("phone_number")
            if num and num not in phone_numbers:
                phone_numbers.append(num)

    primary = api_keys[0] if api_keys else ""
    extras = ",".join(api_keys[1:])
    numbers = ",".join(phone_numbers)
    env = f"""# Generated from Telnyx key audit. Paste into Railway variables after reviewing.
TELNYX_API_KEY={primary}
TELNYX_EXTRA_API_KEYS={extras}
TELNYX_NUMBERS={numbers}
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=300
TELNYX_POLL_LIMIT=10
TELNYX_SYNC_DATE_RANGE=
"""
    Path("railway_values_generated.env").write_text(env, encoding="utf-8")

    summary_lines = [
        "Telnyx API key audit summary",
        f"Total checked: {len(results)}",
        f"Valid with number(s): {len(with_numbers)}",
        f"Valid with no numbers: {len(no_numbers)}",
        f"Invalid/malformed/unauthorized: {len(invalid)}",
        "",
        "Numbers found:",
    ]
    for num in phone_numbers:
        summary_lines.append(f"- {num}")
    summary_lines += [
        "",
        "Files:",
        "- telnyx_valid_with_numbers.csv",
        "- telnyx_valid_no_numbers.csv",
        "- telnyx_invalid_keys.csv",
        "- railway_values_generated.env",
    ]
    Path("telnyx_audit_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/audit_telnyx_keys.py telnyx_keys_input.txt")
        return 2
    input_path = Path(sys.argv[1])
    text = input_path.read_text(encoding="utf-8")
    keys = extract_keys(text)
    print(f"Extracted {len(keys)} unique candidate keys")
    results = []
    for i, key in enumerate(keys, start=1):
        print(f"[{i}/{len(keys)}] checking {mask(key)}", flush=True)
        try:
            results.append(audit_key(i, key))
        except Exception as exc:
            results.append(
                {
                    "index": i,
                    "api_key": key,
                    "api_key_masked": mask(key),
                    "status_code": "exception",
                    "ok": False,
                    "error": str(exc),
                    "numbers": [],
                    "profiles": [],
                }
            )
        time.sleep(0.15)
    write_outputs(results)
    print(Path("telnyx_audit_summary.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
