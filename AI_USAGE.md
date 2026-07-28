# AI usage

I used OpenAI Codex extensively for this assignment: requirements decomposition, Python/SQLite
scaffolding, test-first implementation, edge-case review, performance instrumentation, and editing
the documentation. I explicitly split independent review work across three agents: one challenged
the 100-source architecture, one designed the failure/idempotency test matrix, and one acted as a
strict hiring-panel reviewer. Codex also ran the live collection and benchmark commands.

The decisions I retained and verified against executable evidence were the minimal synchronous
architecture, status-value diffing, transactional snapshot application, restart-persisted rate
limits, content-addressed raw storage, and conservative session censoring. Verification includes
the public feed payload, the SQLite rows, a deterministic local HTTP server, fake-clock cadence
tests, atomic rollback tests, lint/format checks, a clean wheel install, and the 30,000-EVSE
benchmark. Before submitting, I reviewed the code and prose rather than treating generated output
as authoritative.

One concrete suboptimal AI suggestion was to use poll observation timestamps as the sole session
clock. That is robust but throws away the feed's potentially higher-resolution EVSE
`last_updated`. I caught the trade-off while comparing the proposal with the source contract and
hand-built a regressive-timestamp test. The final rule uses source timestamps only when they are
ordered and plausible, otherwise it falls back to observation time and reports the fallback. A
second useful catch during execution was an editable-package console script that failed on a fresh
invocation; the documented setup now uses a normal wheel install and the clean-install check is
part of final verification.
