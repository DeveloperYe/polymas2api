"""Request journal: what the panel shows and what the operator can audit.

The journal records one line per upstream call: which account, which model,
how long it took, how many bytes came back, whether it failed. It deliberately
does **not** store the prompt or the answer by default — those are the
student's course material. The archive is opt-in and, when enabled, is
redacted the same way the outbound body is.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

__all__ = ["Journal", "JournalEntry"]


class JournalEntry:
    """One upstream call."""

    def __init__(self, *, account: str, model: str, stream: bool, prompt_chars: int,
                 duration_ms: int, ok: bool, status: str = "", bytes_in: int = 0,
                 bytes_out: int = 0, error: str = "", trace_id: str = "",
                 reasoning_chars: int = 0, content_chars: int = 0,
                 archived: bool = False, tools: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 aborted: bool = False):
        self.account = account
        self.model = model
        self.stream = stream
        self.prompt_chars = prompt_chars
        self.duration_ms = duration_ms
        self.ok = ok
        self.status = status
        self.bytes_in = bytes_in
        self.bytes_out = bytes_out
        self.error = error
        self.trace_id = trace_id
        self.reasoning_chars = reasoning_chars
        self.content_chars = content_chars
        #: Estimated, never measured — the platform reports no usage
        #: (see :mod:`polymas2api.tokens`).
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        #: The caller hung up mid-answer (a "stop" click). Not an error, but
        #: not a clean success either — counted separately.
        self.aborted = aborted
        self.archived = archived
        #: Names of the *agent's own* tools this call used, comma separated.
        #:
        #: This is an audit trail, not decoration: the agent has a shell
        #: (``执行命令``), so whoever can reach this gateway can ask it to run
        #: commands in the platform's container (verified 2026-11-16). Seeing
        #: them in the log is the cheapest way to notice that.
        self.tools = tools
        self.at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at)),
            "account": self.account,
            "model": self.model,
            "stream": self.stream,
            "ok": self.ok,
            "aborted": self.aborted,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "prompt_chars": self.prompt_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_chars": self.reasoning_chars,
            "content_chars": self.content_chars,
            "tools": self.tools,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "error": self.error,
            "trace_id": self.trace_id,
            "archived": self.archived,
        }


class Journal:
    """A bounded in-memory ring plus an optional on-disk archive."""

    def __init__(self, config, logger=None):
        self.cfg = config
        self.log = logger or (lambda message, **_: None)
        limit = int(getattr(self.cfg, "journal_size", 500) or 500)
        # A negative maxlen makes deque raise, which would leave the panel
        # permanently broken until a restart.
        limit = max(1, min(limit, 100000))
        self._entries: Deque[JournalEntry] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._archive: Optional[Path] = None
        self._archive_enabled = bool(getattr(self.cfg, "archive_enabled", False))
        self._archive_max_bytes = int(getattr(self.cfg, "archive_max_mb", 20) or 20) * 1024 * 1024
        self._counters = {"total": 0, "ok": 0, "failed": 0, "prompt_chars": 0,
                          "content_chars": 0, "reasoning_chars": 0, "bytes_in": 0,
                          "bytes_out": 0, "archive_dropped": 0,
                          "prompt_tokens": 0, "completion_tokens": 0, "aborted": 0}
        #: Lifetime totals read from the archive at startup. ``_counters``
        #: only covers this run, so adding the two gives a real total that
        #: neither resets on restart nor saturates at the ring size.
        self._archive_totals: Dict[str, int] = {
            "total": 0, "ok": 0, "failed": 0, "prompt_chars": 0,
            "content_chars": 0, "reasoning_chars": 0, "bytes_in": 0,
            "bytes_out": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "aborted": 0,
        }
        self._archive_queue: Optional[queue.Queue] = None
        self._archive_thread: Optional[threading.Thread] = None
        self._archive_lock = threading.Lock()
        value = getattr(self.cfg, "archive_file", "") or ""
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = Path(self.cfg.extra.get("_base_dir", ".")) / candidate
            self._archive = candidate
        self._load_archive_tail()

    def _load_archive_tail(self) -> None:
        """Seed the ring buffer from the archive so a restart keeps the history.

        The archive is the only durable record of past calls; without this the
        panel would show an empty log after every restart.
        """
        if self._archive is None or not self._archive.exists():
            return
        # Streamed rather than read whole: the file grows without bound, and
        # only the tail goes into the ring buffer. Everything is still counted,
        # so the totals are lifetime rather than "since this restart".
        tail: "deque[Dict[str, Any]]" = deque(maxlen=self._entries.maxlen)
        try:
            with self._archive.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    totals = self._archive_totals
                    totals["total"] += 1
                    if row.get("ok"):
                        totals["ok"] += 1
                    else:
                        totals["failed"] += 1
                    for name in ("prompt_chars", "content_chars", "reasoning_chars",
                                 "bytes_in", "bytes_out", "prompt_tokens",
                                 "completion_tokens"):
                        totals[name] += int(row.get(name) or 0)
                    if row.get("aborted"):
                        totals["aborted"] += 1
                    tail.append(row)
        except OSError as error:
            self.log(f"cannot read the request archive: {error}")
            return
        for row in tail:
            entry = JournalEntry(
                account=str(row.get("account") or ""),
                model=str(row.get("model") or ""),
                stream=bool(row.get("stream")),
                prompt_chars=int(row.get("prompt_chars") or 0),
                duration_ms=int(row.get("duration_ms") or 0),
                ok=bool(row.get("ok")),
                status=str(row.get("status") or ""),
                bytes_in=int(row.get("bytes_in") or 0),
                bytes_out=int(row.get("bytes_out") or 0),
                error=str(row.get("error") or ""),
                trace_id=str(row.get("trace_id") or ""),
                tools=str(row.get("tools") or ""),
                reasoning_chars=int(row.get("reasoning_chars") or 0),
                content_chars=int(row.get("content_chars") or 0),
                # These three were missing, so every entry restored after a
                # restart came back with zero tokens (found 2026-11-18).
                prompt_tokens=int(row.get("prompt_tokens") or 0),
                completion_tokens=int(row.get("completion_tokens") or 0),
                aborted=bool(row.get("aborted")),
                archived=True,
            )
            entry.at = float(row.get("at") or entry.at)
            self._entries.append(entry)

    # -- recording ----------------------------------------------------------
    def record(self, entry: JournalEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            self._counters["total"] += 1
            if entry.ok:
                self._counters["ok"] += 1
            else:
                self._counters["failed"] += 1
            self._counters["prompt_chars"] += entry.prompt_chars
            self._counters["content_chars"] += entry.content_chars
            self._counters["reasoning_chars"] += entry.reasoning_chars
            self._counters["bytes_in"] += entry.bytes_in
            self._counters["bytes_out"] += entry.bytes_out
            self._counters["prompt_tokens"] += entry.prompt_tokens
            self._counters["completion_tokens"] += entry.completion_tokens
            if entry.aborted:
                self._counters["aborted"] += 1
        if self._archive_enabled and self._archive is not None:
            self._append_archive(entry)

    def _append_archive(self, entry: JournalEntry) -> None:
        """Queue the entry for the archive writer.

        The write happens on a background thread so that a slow disk cannot
        add latency to the request path. A full queue drops the entry and
        counts it — losing an archive line is better than slowing down every
        chat.
        """
        with self._archive_lock:
            if self._archive_queue is None:
                self._archive_queue = queue.Queue(maxsize=20000)
                self._archive_thread = threading.Thread(
                    target=self._archive_worker, name="archive-writer", daemon=True)
                self._archive_thread.start()
            try:
                self._archive_queue.put_nowait(entry)
            except queue.Full:
                with self._lock:
                    self._counters["archive_dropped"] = \
                        self._counters.get("archive_dropped", 0) + 1

    def _archive_worker(self) -> None:
        while True:
            entry = self._archive_queue.get()
            if entry is None:
                return
            try:
                self._write_archive_entry(entry)
            except Exception as error:  # noqa: BLE001
                self.log(f"cannot write the request archive: {error}")

    def _write_archive_entry(self, entry: JournalEntry) -> None:
        try:
            self._archive.parent.mkdir(parents=True, exist_ok=True)
            if (self._archive.exists()
                    and self._archive.stat().st_size > self._archive_max_bytes):
                # Rotate rather than truncate: the previous archive is kept
                # under a numbered name so the history survives.
                index = 1
                while True:
                    candidate = self._archive.with_name(
                        f"{self._archive.stem}.{index}{self._archive.suffix}")
                    if not candidate.exists():
                        break
                    index += 1
                    if index > 50:
                        candidate.unlink(missing_ok=True)
                        break
                self._archive.rename(candidate)
            with self._archive.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError as error:
            self.log(f"cannot write the request archive: {error}")

    # -- reading ------------------------------------------------------------
    def recent(self, limit: int = 100, *, since: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [entry.to_dict() for entry in self._entries if entry.at >= since]
        rows.reverse()
        return rows[:limit]

    def counters(self) -> Dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            # ``_counters`` covers this run only; the archive holds every call
            # ever written to it. Adding the two gives a lifetime total that
            # neither resets on restart nor saturates at the ring buffer size
            # (the panel used to report "500 total" forever — found 2026-11-18).
            for name, value in self._archive_totals.items():
                counters[name] = counters.get(name, 0) + value
        counters["entries"] = len(self._entries)
        counters["archive_enabled"] = self._archive_enabled
        counters["archive_path"] = str(self._archive) if self._archive else ""
        return counters

    def error_summary(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Group failures by message so a pattern is visible at a glance."""
        with self._lock:
            failures = [entry for entry in self._entries if not entry.ok]
        grouped: Dict[str, Dict[str, Any]] = {}
        for entry in failures:
            key = (entry.error or entry.status or "unknown")[:120]
            row = grouped.setdefault(key, {"error": key, "count": 0, "last": 0,
                                           "account": entry.account})
            row["count"] += 1
            row["last"] = max(row["last"], entry.at)
        rows = sorted(grouped.values(), key=lambda row: row["count"], reverse=True)
        for row in rows:
            row["last_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["last"]))
        return rows[:limit]

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            for key in self._counters:
                self._counters[key] = 0
