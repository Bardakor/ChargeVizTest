# ChargeViz EVSE status pipeline

A small, restart-safe pipeline that politely polls Motor Fuel Group's full location snapshot,
records real EVSE status transitions in SQLite, and estimates completed `CHARGING` episodes.
Runtime code uses only the Python standard library.

## Run from a clean machine

Prerequisite: Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install .

chargeviz collect \
  --duration 2h \
  --interval 120 \
  --db data/reviewer.sqlite3 \
  --archive-dir data/reviewer-raw
```

The first request is immediate. Later request **starts** are at least 120 seconds apart, including
after restarts. The process is synchronous; OS locks reject a second collector for either the same
database or source endpoint. Stop safely with `Ctrl-C`; committed snapshots remain valid and an
interrupted attempt remains visible as `RUNNING`. Re-running the command resumes from persisted
EVSE state.

Each poll emits one-line JSON metrics. SQLite contains the current state, immutable initial/change
events, missing-EVSE audit rows, and every successful or failed poll. Successful response bodies
are gzip-compressed under their SHA-256 name; identical snapshots occupy one file.

## Reproduce the result

```bash
chargeviz report --db data/chargeviz.sqlite3
chargeviz report --db data/chargeviz.sqlite3 --format json
```

The supplied `RESULTS.md` explains the session rules and limitations. The included database lets
the report be regenerated exactly; `data/raw/` is intentionally excluded from Git.

## Verify and benchmark

```bash
python -m pip install ".[dev]"
pytest -q
ruff check .
ruff format --check .
python -m chargeviz.benchmark --evses 30000 --iterations 5
```

The tests use local fixtures, a local HTTP server, fake time, temporary databases, and no public
endpoint calls. They cover full-snapshot validation, duplicate conflicts, idempotency, atomic
rollback, restart cadence, 429 handling, gzip transport, missing EVSEs, and session censoring.

## Operational behavior

- A non-2xx response, timeout, malformed/empty JSON, or conflicting duplicate never mutates EVSE
  state.
- `429 Retry-After` is retained separately from the longer enforced delay and never shortens the
  120-second minimum. Repeated failures back off exponentially to 15 minutes.
- Status values—not `last_updated` changes—create events. A missing EVSE creates an audit marker,
  not a fabricated status.
- Source `last_updated` and poll observation time are both retained. All stored times are UTC.
- Compressed and decompressed HTTP bodies are each capped at 64 MiB; timeout defaults to 30 seconds.

Design rationale and the path to 100+ sources are in [`ARCHITECTURE.md`](ARCHITECTURE.md).
