from __future__ import annotations

import gzip
import io
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol


class Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes
    response_bytes: int
    elapsed_ms: float


class RequestFailure(Exception):
    def __init__(
        self,
        *,
        message: str,
        elapsed_ms: float,
        status: int | None = None,
        retry_after_seconds: float | None = None,
        response_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.response_bytes = response_bytes
        self.elapsed_ms = elapsed_ms


class HTTPStatusFailure(RequestFailure):
    pass


class RateLimited(HTTPStatusFailure):
    pass


class NetworkFailure(RequestFailure):
    pass


def parse_retry_after(headers: Headers, *, now: datetime | None = None) -> float | None:
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (deadline.astimezone(UTC) - current.astimezone(UTC)).total_seconds())


class HTTPClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 64 * 1024 * 1024,
        max_decompressed_bytes: int = 64 * 1024 * 1024,
        user_agent: str = "ChargeViz-takehome/1.0",
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive and finite")
        if max_response_bytes <= 0:
            raise ValueError("maximum response size must be positive")
        if max_decompressed_bytes <= 0:
            raise ValueError("maximum decompressed size must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_decompressed_bytes = max_decompressed_bytes
        self.user_agent = user_agent

    def _read_limited(self, response: object) -> bytes:
        body = response.read(self.max_response_bytes + 1)  # type: ignore[attr-defined]
        if len(body) > self.max_response_bytes:
            raise NetworkFailure(
                message=f"response exceeded {self.max_response_bytes} bytes",
                elapsed_ms=0.0,
                response_bytes=len(body),
            )
        return body

    def _decompress_limited(self, wire_body: bytes, elapsed_ms: float) -> bytes:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(wire_body)) as compressed:
                body = compressed.read(self.max_decompressed_bytes + 1)
        except gzip.BadGzipFile as error:
            raise NetworkFailure(
                message="response declared gzip but could not be decompressed",
                elapsed_ms=elapsed_ms,
                response_bytes=len(wire_body),
            ) from error
        if len(body) > self.max_decompressed_bytes:
            raise NetworkFailure(
                message=f"decompressed response exceeded {self.max_decompressed_bytes} bytes",
                elapsed_ms=elapsed_ms,
                response_bytes=len(wire_body),
            )
        return body

    def fetch(self, url: str) -> HTTPResponse:
        started = time.perf_counter()
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                wire_body = self._read_limited(response)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if int(response.status) != 200:
                    raise HTTPStatusFailure(
                        message=f"unexpected HTTP success status {response.status}",
                        status=int(response.status),
                        retry_after_seconds=parse_retry_after(response.headers),
                        response_bytes=len(wire_body),
                        elapsed_ms=elapsed_ms,
                    )
                encoding = response.headers.get("Content-Encoding", "").lower()
                body = (
                    self._decompress_limited(wire_body, elapsed_ms)
                    if encoding == "gzip"
                    else wire_body
                )
                if len(body) > self.max_decompressed_bytes:
                    raise NetworkFailure(
                        message=(
                            f"decompressed response exceeded {self.max_decompressed_bytes} bytes"
                        ),
                        elapsed_ms=elapsed_ms,
                        response_bytes=len(wire_body),
                    )
                return HTTPResponse(
                    status=int(response.status),
                    body=body,
                    response_bytes=len(wire_body),
                    elapsed_ms=elapsed_ms,
                )
        except urllib.error.HTTPError as error:
            elapsed_ms = (time.perf_counter() - started) * 1000
            body = error.read(4096)
            common = {
                "message": f"HTTP {error.code}: {error.reason}",
                "status": int(error.code),
                "retry_after_seconds": parse_retry_after(error.headers),
                "response_bytes": len(body),
                "elapsed_ms": elapsed_ms,
            }
            if error.code == 429:
                raise RateLimited(**common) from error
            raise HTTPStatusFailure(**common) from error
        except RequestFailure:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            elapsed_ms = (time.perf_counter() - started) * 1000
            raise NetworkFailure(
                message=f"network request failed: {error}",
                elapsed_ms=elapsed_ms,
            ) from error
