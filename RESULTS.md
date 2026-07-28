# Results

## Headline

The two-hour collection is in progress. This section will be generated from the final SQLite
database with:

```bash
chargeviz report --db data/chargeviz.sqlite3
```

The reported value will be the arithmetic mean duration of **complete, fully bounded EVSE
`CHARGING` episodes observed during this run**, with the denominator stated explicitly. If none
complete, the result is “not estimable,” never zero.

## Session definition

The entity is `(source, location_id, evse_uid)`; connectors are not independent sessions because
OCPI defines an EVSE as able to charge one vehicle at a time. The rules are:

1. The first valid observation per EVSE establishes a baseline. An EVSE already `CHARGING` is
   left-censored.
2. A transition from any non-`CHARGING` status to `CHARGING` starts a candidate episode.
3. The first later transition to a recognized OCPI status other than `CHARGING` or `UNKNOWN` ends
   it. No duration filters are applied.
4. Literal `UNKNOWN` and unmapped vendor statuses are ambiguous and excluded rather than asserted
   to be clean ends.
5. Right-censored counts cover episodes whose start was observed but whose end was not. Episodes
   already charging at baseline remain in the left-censored category, even if also open at cutoff.
6. Failed polls, missing EVSE rows, or a start-to-start pause over 1.5× cadence create gaps, never
   synthetic ends. Gap-spanning complete episodes remain in the headline mean; a gap-free
   sensitivity mean is reported separately.

Ingestion guarantees parseable UTC timestamps. A source boundary is trusted only when
`previous_observed_at < last_updated <= observed_at` at both start and end, with a positive
duration. Otherwise both boundaries use poll observation time and the fallback is counted. The
all-observation-time mean is also reported. Status is compared directly: OCPI `last_updated` may
also change for connector metadata and is not itself proof of the status-transition instant.

## Interpretation

This measures advertised EVSE “in use” episodes, not billing sessions or continuous energy
transfer. There is no vehicle, customer, kWh, power, authorization, or CDR data. A two-minute
snapshot cadence cannot see an episode that starts and finishes between polls and bounds
observation-time durations to polling resolution.

The short weekday/evening window is one operator, one day, and a convenience sample. Excluding
left/right-censored episodes can favor episodes that fit inside the window, while missed short
episodes can bias in the other direction; the mean is not a population utilization benchmark.
`UNKNOWN`, outages, stale source timestamps, and abnormal terminal statuses require explicit
treatment before business use. P90 uses the nearest-rank convention.

Status semantics follow the official
[OCPI 2.2.1 Locations module](https://github.com/ocpi/ocpi/blob/release-2.2.1-bugfixes/mod_locations.asciidoc);
the feed is treated as OCPI-style rather than assumed fully compliant.

## Processing evidence

The baseline contained 583 locations and 2,944 EVSEs. Its 3.76 MB JSON body compressed to
166.8 KB in the local archive; fetch, parse, and atomic persistence took 21,988.5 ms, 32.9 ms, and
14.4 ms respectively.

| Synthetic workload | Parse p95 | SQLite p95 | Combined p95 | Interval budget | Throughput |
|---|---:|---:|---:|---:|---:|
| 30,000 EVSEs, 5 snapshots, 1% churn | 148.6 ms | 144.2 ms | 273.5 ms | 0.228% | 109,687 EVSE/s |

Environment: Apple Silicon macOS, Python 3.11.13, SQLite from the Python standard library. This
microbenchmark shows local headroom only; it does not predict multi-source network reliability.
