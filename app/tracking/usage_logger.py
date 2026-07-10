from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class UsageLogger:
    """Append-only JSONL audit log, one file per UTC day: tmp/ai/logs/calls-YYYY-MM-DD.jsonl.
    O(1) per write — replaces the original APICallTracker._save_log(), which rewrote the
    entire JSON file on every single call and pruned in-memory. Retention here is handled
    out-of-band (see scripts/prune_logs.py), not synchronously on the hot path.
    """

    def __init__(self, log_dir: str, log_full_payloads: bool = False):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.requests_dir = self.log_dir / "requests"
        self.log_full_payloads = log_full_payloads
        if log_full_payloads:
            self.requests_dir.mkdir(parents=True, exist_ok=True)

    def _calls_file(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"calls-{day}.jsonl"

    def log_call(
        self,
        *,
        request_id: str,
        service: str,
        method: str,
        model: str,
        quota_model: Optional[str],
        api_key_suffix: str,
        success: bool,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        total_tokens: Optional[int],
        response_preview: Optional[str] = None,
        error: Optional[str] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "service": service,
            "method": method,
            "model": model,
            "quota_model": quota_model,
            "api_key_suffix": api_key_suffix,
            "success": success,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "response_preview": response_preview,
            "error": error,
            "latency_ms": latency_ms,
        }
        with open(self._calls_file(), "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def log_error(self, *, request_id: str, message: str, traceback_str: Optional[str] = None) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "message": message,
            "traceback": traceback_str,
        }
        with open(self.log_dir / f"errors-{day}.log", "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def log_full_payload(self, request_id: str, payload: dict[str, Any]) -> None:
        if not self.log_full_payloads:
            return
        with open(self.requests_dir / f"{request_id}.json", "w") as f:
            json.dump(payload, f, indent=2, default=str)
