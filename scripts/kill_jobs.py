#!/usr/bin/env python3
"""Manually clears stuck/orphaned batch jobs from Redis. Useful after killing the
app container mid-batch: leases and queue/processing entries survive in Redis (a
persisted volume), so on next boot the worker pool silently resumes whatever was
still in flight. Run this before bringing the app back up if that's not what you want.

Connects to Redis directly — works whether or not the app container is running.

Usage:
  python scripts/kill_jobs.py                 # purge every batch with anything
                                                # still queued/processing right now
  python scripts/kill_jobs.py --batch <id>     # purge one specific batch (any status)
  python scripts/kill_jobs.py --all            # purge every tracked batch (full wipe)
  python scripts/kill_jobs.py --dry-run        # show what would be purged, no writes
"""
from __future__ import annotations

import argparse
import asyncio

import redis.asyncio as redis

from app.config import get_settings
from app.jobs.store import JobStore
from app.pool.redis_keys import RedisKeys


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    rk = RedisKeys(settings.redis_key_prefix)
    store = JobStore(client, rk, settings)

    try:
        if args.batch:
            if args.dry_run:
                exists = await client.exists(rk.jobs_batch(args.batch))
                print(f"Would purge batch {args.batch}." if exists else f"Batch {args.batch} not found.")
                return
            purged = await store.purge_batch(args.batch)
            print(f"Purged batch {args.batch}." if purged else f"Batch {args.batch} not found.")
            return

        if args.all:
            batch_ids = await client.zrange(rk.jobs_all_batches(), 0, -1)
            if args.dry_run:
                print(f"Would purge {len(batch_ids)} batch(es): {', '.join(batch_ids) or '(none)'}")
                return
            purged = []
            for batch_id in batch_ids:
                if await store.purge_batch(batch_id):
                    purged.append(batch_id)
            print(f"Purged {len(purged)} batch(es).")
            return

        # Default: only batches with something still queued/processing right now.
        queued = await client.lrange(rk.jobs_queue(), 0, -1)
        processing = await client.lrange(rk.jobs_processing(), 0, -1)
        if args.dry_run:
            batch_ids = {e.partition(":")[0] for e in queued + processing}
            print(f"Would purge {len(batch_ids)} pending/stuck batch(es): {', '.join(batch_ids) or '(none)'}")
            return
        purged = await store.purge_pending()
        print(f"Purged {len(purged)} pending/stuck batch(es): {', '.join(purged) or '(none)'}")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--batch", help="Purge one specific batch_id (any status).")
    group.add_argument("--all", action="store_true", help="Purge every tracked batch, regardless of status.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be purged without deleting.")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
