from __future__ import annotations

import gzip
from collections.abc import Iterator
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from chargeviz.collector import CollectorConfig, compute_retry_delay
from chargeviz.http import (
    HTTPClient,
    HTTPStatusFailure,
    NetworkFailure,
    RateLimited,
    parse_retry_after,
)


def test_poll_interval_below_public_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 120"):
        CollectorConfig(interval_seconds=119)


@pytest.mark.parametrize(
    ("retry_after", "consecutive_rate_limits", "expected"),
    [
        (None, 1, 120.0),
        (15.0, 1, 120.0),
        (180.0, 1, 180.0),
        (None, 2, 240.0),
        (None, 10, 900.0),
    ],
)
def test_rate_limit_delay_is_polite_and_bounded(
    retry_after: float | None,
    consecutive_rate_limits: int,
    expected: float,
) -> None:
    assert (
        compute_retry_delay(
            interval_seconds=120.0,
            retry_after_seconds=retry_after,
            consecutive_rate_limits=consecutive_rate_limits,
        )
        == expected
    )


def test_retry_after_delta_seconds_is_parsed() -> None:
    headers = Message()
    headers["Retry-After"] = "45"

    assert parse_retry_after(headers) == 45.0


def test_duration_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        CollectorConfig(duration_seconds=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_seconds", float("nan")),
        ("interval_seconds", float("inf")),
        ("duration_seconds", float("nan")),
        ("duration_seconds", float("inf")),
    ],
)
def test_collector_config_rejects_non_finite_values(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        CollectorConfig(**{field: value})


def test_non_finite_retry_after_is_ignored() -> None:
    headers = Message()
    headers["Retry-After"] = "Infinity"

    assert parse_retry_after(headers) is None


@pytest.fixture
def local_http_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/limited":
                self.send_response(429)
                self.send_header("Retry-After", "15")
                self.end_headers()
                self.wfile.write(b'{"message":"slow down"}')
                return
            if self.path == "/error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"unavailable")
                return
            if self.path == "/partial":
                self.send_response(206)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
                return
            body = gzip.compress(b'{"data":[]}')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_http_client_decompresses_a_successful_response(local_http_server: str) -> None:
    response = HTTPClient(timeout_seconds=2).fetch(f"{local_http_server}/success")

    assert response.status == 200
    assert response.body == b'{"data":[]}'
    assert response.response_bytes > 0
    assert response.elapsed_ms >= 0


def test_http_client_exposes_rate_limit_without_retrying(local_http_server: str) -> None:
    with pytest.raises(RateLimited) as captured:
        HTTPClient(timeout_seconds=2).fetch(f"{local_http_server}/limited")

    assert captured.value.status == 429
    assert captured.value.retry_after_seconds == 15.0
    assert captured.value.response_bytes == 23


def test_http_client_exposes_non_retrying_http_failure(local_http_server: str) -> None:
    with pytest.raises(HTTPStatusFailure) as captured:
        HTTPClient(timeout_seconds=2).fetch(f"{local_http_server}/error")

    assert captured.value.status == 503


def test_http_client_rejects_partial_success_response(local_http_server: str) -> None:
    with pytest.raises(HTTPStatusFailure) as captured:
        HTTPClient(timeout_seconds=2).fetch(f"{local_http_server}/partial")

    assert captured.value.status == 206


def test_http_client_caps_decompressed_response_size(local_http_server: str) -> None:
    with pytest.raises(NetworkFailure, match="decompressed"):
        HTTPClient(
            timeout_seconds=2,
            max_response_bytes=1024,
            max_decompressed_bytes=5,
        ).fetch(f"{local_http_server}/success")
