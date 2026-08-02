# AI usage

I used two assistants, at two different stages, and they did a lot of the typing.

**OpenAI Codex** built the pipeline. I used it for requirements decomposition, the Python and SQLite
scaffolding, test-first implementation, edge-case review, the performance instrumentation, and it
ran the live two-hour collection and the benchmark. I deliberately split the review work across
three separate agents so they would not agree with each other by default: one attacked the
100-source architecture, one designed the failure and idempotency test matrix, and one played a
hostile hiring panel.

**Claude Code (Opus)** came later, for the writing and a cleanup pass. It rewrote the four markdown
files, wrote the French glossary, and did the simplification the code needed after the fact —
collapsing five nearly identical exception handlers in the collector into one classifier, deleting
migration code for a schema that had never shipped, and removing a `persist_ms` field that was
passed as zero everywhere and then overwritten.

What I decided rather than accepted: the minimal synchronous architecture, diffing on status
*values* instead of `last_updated`, transactional snapshot application, rate limits that survive a
restart, content-addressed raw storage, and the conservative censoring rules in `RESULTS.md`. The
evidence behind those decisions is executable — the public feed payload, the rows in SQLite, a local
HTTP server, fake-clock cadence tests, atomic rollback tests, lint and format checks, a clean wheel
install, and the 30,000-EVSE benchmark. Before submitting I read the code and the prose rather than
trusting that generated output was right because it looked right.

**Where the AI was wrong.** Two cases worth naming.

The first was a design call. Codex proposed using poll observation timestamps as the only session
clock. That is robust, and it is also lazy: it throws away the feed's own `last_updated`, which is
potentially higher-resolution. I caught it comparing the proposal against the source contract and
hand-wrote a regressive-timestamp test. The rule that shipped trusts source timestamps only when
they are ordered and plausible at both ends of an episode, falls back to observation time otherwise,
and reports how often it fell back. In this run it fell back twice out of 519, and the two clocks
land 1.9 seconds apart — which is itself a result worth having.

The second was a number, and it is the one that bothers me more. `ARCHITECTURE.md` claimed a
benchmark p95 of 610–681 ms. When I re-ran that benchmark three times before submitting, I got
273–284 ms, consistently. The old figure did not reproduce on my machine, and I could not
reconstruct the conditions that produced it. So it is gone, replaced with what I actually measured
and can re-measure. A number in a document that nobody re-runs is a number that quietly rots, and I
would rather ship a smaller claim I can defend. In the same pass I noticed the report command was
running the entire analysis twice for markdown output, because `render_markdown` called `analyze`
on a path the caller had already analysed. That one was pure AI-written waste and it survived
because the tests only checked the output, not the work done to get it.

There is a general lesson in the second case that applies well beyond a take-home. This whole
exercise is about turning a regulatory data feed into a number that someone will act on — a site
investment, a maintenance rota, a coverage obligation. Assistants are very good at producing
confident numbers. The part that has to stay human is asking whether the number still reproduces
when you run it again.
