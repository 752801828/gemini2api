"""Structured log store with circular buffer, filtering, pagination, and persistence."""

import json
import re
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional


_TOKEN_SCRUB_RE = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)
_SK_SCRUB_RE = re.compile(r"sk-[A-Za-z0-9]{4,}")
_BEARER_SCRUB_RE = re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE)
_DATA_URL_RE = re.compile(
    r"data:(image|audio|video)/[^;,\s]+;base64,[A-Za-z0-9+/=_-]+",
    re.IGNORECASE,
)
_SENSITIVE_LOG_KEYS = {
    "authorization", "api_key", "apikey", "x_api_key", "key", "access_token",
    "refresh_token", "id_token", "token", "secret", "client_secret", "password",
    "cookie", "cookies",
}
_MEDIA_LOG_KEYS = {"b64_json", "base64", "image_base64", "audio_base64", "video_base64"}
LOG_BODY_CAPTURE_LIMIT = 64 * 1024
_LOG_PAYLOAD_CHAR_LIMIT = 20_000
_LOG_STRING_CHAR_LIMIT = 12_000


def scrub_log_text(text: str) -> str:
    if not text:
        return text
    scrubbed = _TOKEN_SCRUB_RE.sub(r"\1****", text)
    scrubbed = _SK_SCRUB_RE.sub("sk-****", scrubbed)
    return _BEARER_SCRUB_RE.sub(r"\1****", scrubbed)


def sanitize_log_payload(value, depth: int = 0):
    """Preserve useful JSON while removing secrets and large media blobs."""
    if depth > 10:
        return "[nested content omitted]"
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            name = str(key)
            normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
            if normalized in _SENSITIVE_LOG_KEYS:
                sanitized[name] = "****"
            elif normalized in _MEDIA_LOG_KEYS and isinstance(item, str):
                sanitized[name] = f"[base64 omitted: {len(item)} chars]"
            else:
                sanitized[name] = sanitize_log_payload(item, depth + 1)
        return sanitized
    if isinstance(value, list):
        return [sanitize_log_payload(item, depth + 1) for item in value]
    if isinstance(value, str):
        text = scrub_log_text(value)
        text = _DATA_URL_RE.sub(
            lambda match: f"[{match.group(1).lower()} base64 omitted: {len(match.group(0))} chars]",
            text,
        )
        if len(text) > _LOG_STRING_CHAR_LIMIT:
            return text[:_LOG_STRING_CHAR_LIMIT] + f"...[truncated {len(text) - _LOG_STRING_CHAR_LIMIT} chars]"
        return text
    return value


def limit_log_payload(value):
    sanitized = sanitize_log_payload(value)
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= _LOG_PAYLOAD_CHAR_LIMIT:
        return sanitized
    return {
        "truncated": True,
        "preview": serialized[:_LOG_PAYLOAD_CHAR_LIMIT],
        "omitted_chars": len(serialized) - _LOG_PAYLOAD_CHAR_LIMIT,
    }


def decode_logged_response(captured: bytes, content_type: str, stream: bool | None, truncated: bool) -> dict:
    text = captured.decode("utf-8", errors="replace")
    if "application/json" in content_type and not truncated:
        try:
            return limit_log_payload(json.loads(text))
        except json.JSONDecodeError:
            pass
    return {
        "stream": bool(stream or "text/event-stream" in content_type),
        "content_type": content_type or None,
        "truncated": truncated,
        "preview": sanitize_log_payload(text),
    }


async def capture_response_iterator(body_iterator, log_store, record_id: str, content_type: str, stream: bool | None):
    captured = bytearray()
    truncated = False
    try:
        async for chunk in body_iterator:
            raw = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            remaining = LOG_BODY_CAPTURE_LIMIT - len(captured)
            if remaining > 0:
                captured.extend(raw[:remaining])
            if len(raw) > remaining:
                truncated = True
            yield chunk
    finally:
        updater = getattr(log_store, "update_response", None)
        if updater:
            updater(
                record_id,
                decode_logged_response(bytes(captured), content_type, stream, truncated),
            )


@dataclass
class LogRecord:
    id: str
    request_id: str
    direction: str
    ts: str
    method: str
    path: str
    model: Optional[str] = None
    provider: Optional[str] = None
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    stream: Optional[bool] = None
    error: Optional[str] = None
    tags: list = field(default_factory=list)
    request: Optional[dict] = None
    response: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def matches_search(self, query: str) -> bool:
        q = query.lower()
        searchable = [
            self.path,
            self.method,
            self.model or "",
            self.error or "",
            str(self.status) if self.status else "",
        ]
        return any(q in s.lower() for s in searchable)


@dataclass
class LogState:
    enabled: bool = True
    paused: bool = False


class LogStore:
    def __init__(self, capacity: int = 2000, persist_path: str = "data/logs.json"):
        self._buffer: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = Lock()
        self._state = LogState()
        self._id_index: dict[str, LogRecord] = {}
        self._persist_path = Path(persist_path)
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for item in raw:
                record = LogRecord(**item)
                self._buffer.append(record)
                self._id_index[record.id] = record
        except Exception:
            pass

    def flush(self) -> None:
        if not self._dirty:
            return
        with self._lock:
            data = [r.to_dict() for r in self._buffer]
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            self._dirty = False
        except Exception:
            pass

    @property
    def state(self) -> LogState:
        return self._state

    def add(self, record: LogRecord) -> None:
        if not self._state.enabled or self._state.paused:
            return
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                evicted = self._buffer[0]
                self._id_index.pop(evicted.id, None)
            self._buffer.append(record)
            self._id_index[record.id] = record
        self._dirty = True

    def update_response(self, record_id: str, response_body: dict) -> None:
        """Attach a captured response to its existing request record."""
        with self._lock:
            record = self._id_index.get(record_id)
            if record is None:
                return
            record.response = response_body
        self._dirty = True

    def query(
        self,
        limit: int = 50,
        offset: int = 0,
        direction: str = "all",
        search: str = "",
    ) -> dict:
        with self._lock:
            records = list(reversed(self._buffer))

        if direction != "all":
            records = [r for r in records if r.direction == direction]

        if search:
            records = [r for r in records if r.matches_search(search)]

        total = len(records)
        page = records[offset : offset + limit]
        return {
            "records": [r.to_dict() for r in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get(self, record_id: str) -> Optional[dict]:
        with self._lock:
            record = self._id_index.get(record_id)
        if record:
            return record.to_dict()
        return None

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._id_index.clear()
        self._dirty = True
        self.flush()

    def get_state(self) -> dict:
        return {"enabled": self._state.enabled, "paused": self._state.paused}

    def set_state(self, enabled: Optional[bool] = None, paused: Optional[bool] = None) -> dict:
        if enabled is not None:
            self._state.enabled = enabled
        if paused is not None:
            self._state.paused = paused
        return self.get_state()


def create_log_record(
    method: str,
    path: str,
    direction: str = "ingress",
    model: Optional[str] = None,
    status: Optional[int] = None,
    latency_ms: Optional[float] = None,
    stream: Optional[bool] = None,
    error: Optional[str] = None,
    request_body: Optional[dict] = None,
    response_body: Optional[dict] = None,
) -> LogRecord:
    now = datetime.now(timezone.utc).isoformat()
    return LogRecord(
        id=uuid.uuid4().hex[:12],
        request_id=uuid.uuid4().hex[:8],
        direction=direction,
        ts=now,
        method=method,
        path=path,
        model=model,
        provider="gemini",
        status=status,
        latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
        stream=stream,
        error=error,
        request=request_body,
        response=response_body,
    )
