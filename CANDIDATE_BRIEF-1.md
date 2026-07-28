# Take-Home Assignment — Data Engineer

Thank you for your interest in the role. This assignment mirrors a real problem we solve every day: turning raw, public charge-point availability data into business-grade metrics.

**Expected effort:** ~2–3 hours of hands-on work, plus a ~2-hour data-collection run of your script (you can do other things while it runs). Total wall-clock: plan for half a day. Please do not spend more than this — we grade judgment, not volume.

**AI tools (Claude Code, Cursor, Copilot, ChatGPT, etc.) are explicitly allowed and encouraged.** We use them daily and expect you to. See the disclosure requirement below.

---

## Context

We are a data intelligence platform for EV charging in Europe. Our clients (charge point operators, investors, public authorities) rely on us to answer questions like: *how are charging sites actually being used?* The raw material for that answer is real-time charge-point status data.

## The task

1. **Build a small pipeline** that polls a public real-time availability feed for EV charge points, detects status changes, and records them.

   - Data source: `https://opendata.motorfuelgroup.net/locations` — the open-data endpoint of Motor Fuel Group (UK charge point operator), published under the UK Public Charge Point Regulations. It returns the full list of locations, their EVSEs (individual charge points) and connectors in an OCPI-style JSON format. Each EVSE carries a `status` and a `last_updated` timestamp. Refer to the OCPI specification for status semantics.
   - The endpoint returns a **full snapshot** on every call — there is no delta/changes API. Detecting what changed is your job.
   - **The endpoint is rate-limited and will return HTTP 429 if you poll too aggressively.** Poll at most once every 2 minutes, never in parallel, and handle rate-limit responses gracefully. Treat this public endpoint with the same courtesy you'd want for your own API — a banned IP is not a valid excuse in your `RESULTS.md`, but a documented, well-handled outage window is.

2. **Run it for approximately 2 hours**, during daytime hours (UK time) on a weekday if possible.

3. **Reconstruct charging sessions** from the recorded status changes. There is no official definition of a "session" in this data — defining one is part of the exercise. Document your rules.

4. **Report the average session duration** observed during your run, along with your interpretation of that result: what it does and does not tell us, and anything we should know before using it.

## The architecture constraint

Your pipeline will ingest **one** source. In production, we ingest data from **100+ sources**, with heterogeneous schemas, formats (CSV, JSON, NDJSON, XML), and delivery methods (polling, push/webhook, file drops), at different frequencies.

Write a **one-page maximum** `ARCHITECTURE.md` explaining how your design would extend to that reality: what stays generic, what is per-source, and what you would change first. We will compare this document against your actual code, so make sure the two tell the same story.

## Deliverables

A Git repository (link or archive) containing:

| File | Content |
|---|---|
| `README.md` | How to run it, from a clean machine, in under 5 minutes of reading. |
| Your code | Python preferred, but use what you're best at. **All code comments in English.** |
| `ARCHITECTURE.md` | One page max. The 100+ sources answer. |
| `RESULTS.md` | Your average session duration, how you computed it, your session definition rules, and your interpretation of the result. |
| `AI_USAGE.md` | See below. |

### AI usage disclosure (`AI_USAGE.md`)

A few honest paragraphs:
- Which tools you used and for what (scaffolding, debugging, design discussion, writing…).
- What you wrote or verified yourself.
- One concrete case where the AI was wrong or suboptimal and how you caught it.

This is not a trap and there is no "correct" amount of AI usage.

## Practical rules

- Keep infrastructure minimal: a script + SQLite/PostgreSQL/files is perfectly fine. We do not expect (and will not reward) orchestrators, message queues, or cloud deployments for this scope.
- If the feed misbehaves during your run (429s, gaps, stale `last_updated` values, transient errors), that's real life — handle it or document it. A run with a visible, well-handled gap is worth more than a suspiciously perfect one.
- Don't include any credentials or personal data in the repo.
- If something in this brief is ambiguous, make a reasonable decision and document it. 

## What happens next

If we move forward, we'll spend one hour together in person:
- ~20 min: you walk us through what you built; we'll have read your code beforehand and will challenge your choices.
- ~20 min: we show you (a simplified view of) how we solve this problem today.
- ~20 min: you tell us what you'd improve in our approach, what you'd tackle first, and why.

Good luck — we're looking forward to reading your code.
