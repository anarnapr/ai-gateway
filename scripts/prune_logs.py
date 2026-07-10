#!/usr/bin/env python3
"""Deletes call/error log files under tmp/ai/logs older than --days. Run out-of-band
(e.g. via cron), since usage_logger.py intentionally does not prune synchronously on
the request hot path (that per-call full-file-rewrite pattern is exactly what this
service replaced from the original APICallTracker).

Usage: python scripts/prune_logs.py [--log-dir tmp/ai/logs] [--days 14]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default="tmp/ai/logs")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    cutoff = time.time() - args.days * 86400
    log_dir = Path(args.log_dir)
    removed = 0
    for pattern in ("calls-*.jsonl", "errors-*.log", "requests/*.json"):
        for path in log_dir.glob(pattern):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1

    print(f"Removed {removed} log file(s) older than {args.days} days from {log_dir}.")


if __name__ == "__main__":
    main()
