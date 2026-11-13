# AI Usage Disclosure

## Tools used
- Antigravity / Gemini 3.8 Flash

## What I used them for
- Scaffolding the initial project layout and directory structure for Assignment A.
- Reviewing regular expression patterns for diverse phone number formats (`91-`, `0`, `(+91)`) and currency symbols (`₹`, `Rs`, `Rs.`, `INR`).
- Generating boilerplate for Pytest test cases covering date parsing, phone formatting, and schema validation.
- Drafting initial documentation templates for `SUBMISSION.md`.

## What I wrote or decided myself
- **Database & Ledger Architecture:** Choosing SQLite with an append-only `events` ledger and a staging `orphan_events` table for handling out-of-order drop events.
- **PII Protection Strategy:** Designing the SHA-256 phone hashing mechanism to enable relational joins between leads and events without keeping raw phone numbers in plaintext.
- **Derived Metrics & Aggregation Engine:** Implementing the exact hierarchy for lead bucket resolution (`yet_to_reach_out` $\rightarrow$ `completed` $\rightarrow$ `in_progress_telephony_failed` $\rightarrow$ `in_progress_dialed`) and effective disposition override semantics.
- **Idempotency Model:** Tracking ingested files and enforcing unique constraints to guarantee that duplicate ingestion runs produce byte-identical outputs.

## Where the AI was wrong or unhelpful
- Initially, the generated code for date parsing used `%d-%b-%Y` without case normalization, which failed on lowercase month strings like `01-dec-2026` or uppercase `01-DEC-2026`. I identified this during unit testing and added title-casing (`s.title()`) for month-based formats to handle all casing variations properly.
- The AI initially suggested storing unlinked events in memory during the ingest run; I rejected this because events in `drop_1` whose leads arrive in `drop_2` would be lost across separate CLI invocations, and instead implemented the persistent SQLite `orphan_events` staging table.

## How I verified AI-generated code
- **Automated Testing:** Wrote and executed 22 unit tests using `pytest` covering normalizers, schema loader, database operations, orphan reconciliation, and report generation.
- **End-to-End Simulation:** Tested the complete pipeline against synthetic datasets generated with `--seed 42` and unseen `--seed 999`, verifying that all five report files matched the output contract and that re-ingesting drops was completely idempotent.
- **Manual Code Review:** Inspected every line of code to ensure strict adherence to the take-home specifications.
