#!/usr/bin/env python3
"""Rich CLI dashboard, ported from the source repo's services/support/quota_check.py.
Unlike the original (which read local tmp/pool/*.json + in-process objects directly),
this hits the running service's HTTP API — the dashboard can point at any deployment.

Usage:
    python scripts/quota_dashboard.py [--url http://localhost:8080] [--provider gemini] [--watch] [--interval 5]
"""
from __future__ import annotations

import argparse
import time

import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()


def build_table(base_url: str, provider: str) -> Table:
    with httpx.Client(timeout=10.0) as client:
        pool_resp = client.get(f"{base_url}/v1/pool/status", params={"provider": provider})
        pool_resp.raise_for_status()
        status = pool_resp.json()

        keys_resp = client.get(f"{base_url}/v1/keys", params={"provider": provider})
        keys_resp.raise_for_status()
        keys = keys_resp.json()

    table = Table(title=f"{provider} key pool — model {status['model']}")
    table.add_column("Suffix")
    table.add_column("Status")
    table.add_column("Reason")
    table.add_column("Retry in (s)", justify="right")

    for entry in keys:
        style = {
            "available": "green",
            "in_use": "cyan",
            "dead_auth": "bold red",
            "dead_quota": "red",
            "rate_limited": "yellow",
            "high_demand": "yellow",
            "short_cooldown": "yellow",
            "tracker_limited": "yellow",
        }.get(entry["status"], "white")
        table.add_row(
            f"...{entry['suffix']}",
            f"[{style}]{entry['status']}[/{style}]",
            entry.get("reason") or "-",
            f"{entry['retry_in_seconds']:.0f}",
        )

    table.caption = (
        f"{status['available']}/{status['total_keys']} available, "
        f"{status['in_use']} in-use, {status['permanently_blocked']} permanently blocked "
        f"(max {status['in_flight_limit']} in-flight)"
    )
    return table


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    if not args.watch:
        console.print(build_table(args.url, args.provider))
        return

    with Live(build_table(args.url, args.provider), refresh_per_second=1 / args.interval, console=console) as live:
        while True:
            time.sleep(args.interval)
            live.update(build_table(args.url, args.provider))


if __name__ == "__main__":
    main()
