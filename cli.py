"""Command-line interface for the SMS toolkit (Telnyx-backed).

Examples
--------
Send one SMS:
    python cli.py send --to +12015550123 --body "Hello"

Send an OTP, then verify it later:
    python cli.py otp-send --to +12015550123
    python cli.py otp-verify --to +12015550123 --code 123456

Bulk campaign from a CSV with a `phone` column:
    python cli.py bulk-send --csv recipients.csv --body "Update..." --confirm-opt-in
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from config import Config, load_config
from otp import OTPService
from telnyx import SendResult, TelnyxClient


def _print_results(results: list[SendResult]):
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Send results")
        table.add_column("To")
        table.add_column("From")
        table.add_column("Status")
        table.add_column("Message ID")
        table.add_column("Error")
        for r in results:
            table.add_row(
                r.to_number,
                r.from_number,
                r.status,
                r.message_id or "-",
                r.error or "-",
            )
        console.print(table)
        ok = sum(1 for r in results if r.ok)
        console.print(f"[green]{ok}/{len(results)} accepted[/green]")
    except ImportError:
        for r in results:
            mark = "OK " if r.ok else "ERR"
            print(
                f"[{mark}] to={r.to_number} from={r.from_number} "
                f"status={r.status} id={r.message_id or ''} error={r.error or ''}"
            )


def _from_number(args, config: Config) -> str:
    value = getattr(args, "from_number", None) or config.telnyx_from_number
    if not value:
        raise SystemExit(
            "Missing sender number. Use --from +1... or set TELNYX_FROM_NUMBER in .env"
        )
    return value


def cmd_send(args, client: TelnyxClient, config: Config):
    result = client.send_message(_from_number(args, config), args.to, args.body)
    _print_results([result])
    return 0 if result.ok else 1


def cmd_bulk_send(args, client: TelnyxClient, config: Config):
    if not args.confirm_opt_in and not args.dry_run:
        raise SystemExit(
            "Bulk send requires --confirm-opt-in. Only text recipients who consented."
        )

    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"CSV not found: {path}")

    from_number = _from_number(args, config)
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    numbers = [str(row.get(args.to_column, "")).strip() for row in rows]
    numbers = [n for n in numbers if n]
    if not numbers:
        raise SystemExit(f"No recipients found in column {args.to_column!r}")

    if args.dry_run:
        print(f"DRY RUN: would send to {len(numbers)} recipients from {from_number}")
        for n in numbers[:25]:
            print(n)
        if len(numbers) > 25:
            print(f"... and {len(numbers) - 25} more")
        return 0

    results: list[SendResult] = []
    for to_number in numbers:
        results.append(client.send_message(from_number, to_number, args.body))
        if args.delay > 0:
            time.sleep(args.delay)
    _print_results(results)
    return 0 if all(r.ok for r in results) else 1


def cmd_otp_send(args, otp: OTPService, config: Config):
    result = otp.send(_from_number(args, config), args.to)
    _print_results([result])
    return 0 if result.ok else 1


def cmd_otp_verify(args, otp: OTPService):
    ok = otp.verify(args.to, args.code)
    print("VERIFIED" if ok else "INVALID or EXPIRED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SMS toolkit (Telnyx-backed).")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("send", help="Send one outbound SMS")
    s.add_argument("--from", dest="from_number", help="Your Telnyx number, E.164")
    s.add_argument("--to", required=True, help="Recipient number, E.164")
    s.add_argument("--body", required=True, help="Message text")
    s.set_defaults(func=cmd_send)

    b = sub.add_parser("bulk-send", help="Send the same SMS to CSV recipients")
    b.add_argument("--from", dest="from_number", help="Your Telnyx number, E.164")
    b.add_argument("--csv", required=True, help="CSV file with recipient numbers")
    b.add_argument("--to-column", default="phone", help="CSV column name (default: phone)")
    b.add_argument("--body", required=True, help="Message text")
    b.add_argument("--delay", type=float, default=0.25, help="Seconds between sends")
    b.add_argument("--dry-run", action="store_true", help="Preview recipients only")
    b.add_argument(
        "--confirm-opt-in",
        action="store_true",
        help="Required for real bulk sends: recipients consented to receive SMS",
    )
    b.set_defaults(func=cmd_bulk_send)

    o = sub.add_parser("otp-send", help="Send a one-time passcode")
    o.add_argument("--from", dest="from_number", help="Your Telnyx number, E.164")
    o.add_argument("--to", required=True, help="Recipient number, E.164")
    o.set_defaults(func=cmd_otp_send)

    v = sub.add_parser("otp-verify", help="Verify a one-time passcode")
    v.add_argument("--to", required=True, help="Recipient number, E.164")
    v.add_argument("--code", required=True, help="The code to verify")
    v.set_defaults(func=cmd_otp_verify)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    client = TelnyxClient(config.telnyx_api_key, config.telnyx_base_url)
    otp = OTPService(config, client)

    if args.command in ("send", "bulk-send"):
        return args.func(args, client, config)
    if args.command == "otp-send":
        return args.func(args, otp, config)
    return args.func(args, otp)


if __name__ == "__main__":
    sys.exit(main())
