# Architecture

This solution is intentionally one synchronous Python process plus SQLite. `collect` schedules a
request with a monotonic clock, fetches one full snapshot, and passes the bytes to the MFG adapter.
The adapter validates the source shape and emits canonical observations:
`(source, location_id, evse_uid, evse_id, status, source_last_updated)`. A generic reducer compares
that complete set with `current_evse_state` and, in one transaction, appends genuine status events,
updates state/presence, and marks the poll successful. Network I/O and parsing happen outside the
write transaction.

The core invariants are: endpoint/database locks allow one collector; one request is in flight;
request starts are at least 120 seconds apart; first sight is a baseline, not a change; a change
exists only when status differs; missing is not a status; failed or invalid snapshots do not alter
state; and a committed snapshot is all-or-nothing. Every attempt records outcome, counts, hash,
timings, and error. Raw JSON is content-addressed and compressed, preserving replay input without
storing identical snapshots repeatedly. Sessions are derived from immutable events, so their
rules can change without recollecting data.

| Generic across sources | Per source |
|---|---|
| Scheduling policy, attempt ledger, backoff, structured logs | Delivery adapter: poll, webhook, or file reader |
| Raw hashing/archive contract | CSV/JSON/NDJSON/XML parsing and schema validation |
| Canonical EVSE observation and transactional reducer | External identity and status mapping |
| Event/session transforms and quality metrics | Full-snapshot absence and timestamp-trust rules |

For 100+ feeds, I would keep those boundaries and add a source registry containing adapter,
frequency, timeout, and rate-limit configuration. Independent source workers may run concurrently,
but a per-source lease must preserve serial execution. Push and file adapters would land immutable
payloads and invoke the same normalize/reduce path.

The first infrastructure change would be PostgreSQL for concurrent writers, operational queries,
and database-backed leases, plus object storage for raw payloads. Partitioning by source and event
date follows measured volume. I would add a queue only when push bursts, retry isolation, or
independent scaling requires it—not merely because there are 100 sources. Schema contracts,
replay tests, source-level freshness/error SLOs, and quarantine for bad payloads matter before a
larger scheduler.

Throughput is not the present constraint: a 30,000-EVSE synthetic snapshot (10.2× the live feed)
used 273.5 ms p95 for parse plus SQLite reduction, 0.228% of the 120-second interval. The harder
production problems are semantic: stable identity, heterogeneous status meaning, partial
snapshots, source timestamp quality, and unseen transitions between polls.
