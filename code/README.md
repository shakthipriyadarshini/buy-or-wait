# Buy or Wait? — approach

## Core idea
Deterministic financial simulation, not free-form LLM reasoning. The model
touches only two things:
1. **Extraction** (`engine/extract.py`) — messages/images → a validated,
   closed-schema signal against a *specific* event. Messages with no
   explicit `related_event_id` (~35% of them) get a short keyword-matched
   candidate list (`engine/message_targeting.py`); the model picks among
   those candidates, it can't invent a new one.
2. **Explanation** (`engine/explain.py`) — captions a decision the engine
   already made; told not to recompute anything.

Everything numeric — currency conversion, recurrence detection, the 90-day
simulation, `amount_safe_to_pay`, `earliest_date_for_full_payment`, plan
ranking — is deterministic Python.

## Why there's a whole recurrence.py module
The dataset has **no** `is_recurring`/`recurrence_days` field — just 25k+
raw historical transactions. Recurring expenses/income are detected in
three tiers, in order, per category:
1. **Per-(category, description) periodicity** — a single named bill
   (rent, one specific subscription). Projects forward at the literal
   last observed amount, so a message-driven amendment (a raise, a plan
   change) propagates exactly.
2. **Pooled per-category periodicity**, across whatever tier 1 didn't
   catch — real spending that rotates between several merchants for the
   same need (e.g. groceries at 7 different stores, but always roughly
   every 10 days) that no single merchant's history alone would ever
   reach 3 points for. Added after finding a real user (in
   `sample_requests.csv`) whose clearly-periodic groceries were being
   completely missed because no single store name repeated enough.
3. **Smoothed average daily rate**, for whatever's still genuinely
   irregular after both tiers — dining, one-off transport, etc. Debit-only
   (see the bug note below); never offered as a `spending_changes_needed`
   target since it's a pooled estimate, not one named commitment.

Monthly-cadence series (tier 1 or 2) project on the same **calendar day
each month**, not a fixed 30-day step — a fixed-day step drifts away from
the true date over a 90-day window (a real bug, caught by diffing against
`sample_requests.csv` and fixed).

**A real bug worth naming**: the first version of tier 3 computed the
smoothed amount from the raw unsigned `amount` field and force-applied a
debit (negative) sign to *everything* in the leftover pool — including
irregular **income**. For any user whose salary happened to miss the
periodicity test, that meant treating their paycheck as a large recurring
*expense*. Found by tracing one user's forecast line-by-line after a
regression, not by inspection — a reminder that "it runs and produces
plausible-looking numbers" is not the same as correct, and that this kind
of bug hides well behind a working invariant checker (the checker validates
structure, not sign correctness). Fixed by restricting tier 3 to
`direction == "debit"` only, which is also the philosophically correct
call for a safety-margin system: uncertain patterns should only ever add
assumed caution, never assumed extra safety.

**Tier 2's amount rule was tuned empirically, against my own intuition**:
initially projected tier 2 at an average of the last 5 occurrences,
reasoning that a single grocery run's price is noisier than one named
bill's amount. Measured it against `sample_requests.csv` both ways —
averaging helped two individual rows close their gap but made the
*aggregate* `amount_safe_to_pay` error worse (164.6k → 177.1k MAE) by
moving other rows further from gold. Reverted to the same rule as tier 1
(literal last value) on the strength of that measurement, not the
intuition that motivated trying it.

## Flexibility classification
Each event carries a `flexibility` tag (`fixed`/`stoppable`/`reducible`/
`reducible_or_stoppable`) — but that's the *mechanism*, not *permission*.
`conflict_resolution.classify_flexibility` combines it with the user's
profile category lists (`expense_categories_to_protect` /
`..._willing_to_reduce` / `..._willing_to_stop`): a protected category is
never touched regardless of the event's own tag — protect always wins.

## Spending-change search
`engine/spending_changes.py` — when a plan fails the 90-day check on its
own, this does a bounded search (≤3 changes, ≤8-candidate pool) over
stop/reduce combinations on flexible recurring series to see if any rescue
it, rather than a hand-written per-case heuristic.

## LLM backend (`llm_client.py`)
Google Gemini via `google-genai` (free tier, no billing), behind the same
`call_llm`/`call_vlm` interface the engine expects — swapping providers
again means editing only this file. Built up through several rounds of
hitting free-tier limits for real and fixing the actual cause each time,
not just adding a longer sleep:
- **Proactive per-minute rate limiting** — throttles *before* a call would
  exceed the configured budget, instead of firing and reacting to 429s.
- **Daily-quota fast-fail** — RPM and RPD limits both surface as the same
  429, but only RPM heals itself in under a minute. If every retry for one
  call still 429s, the run assumes the daily quota is gone and stops
  paying the retry/backoff cost on every remaining call for the rest of
  the process, falling straight to the mock instead.
- **Hard, externally-enforced timeout** — the SDK's own `timeout` setting
  is unreliable (a documented upstream bug: requests can stall
  indefinitely regardless of what's configured). Every call runs in a
  worker thread with a deadline enforced from *outside* the SDK via
  `Future.result(timeout=...)`, so a stalled call can't block the batch
  forever.
- **On-disk response cache** (`.llm_cache.json`) — keyed by a hash of the
  exact request. Re-running `main.py` (e.g. after a daily quota reset, or
  while iterating on unrelated code) never re-pays for an extraction that
  already succeeded.
- `LAST_CALL_SOURCE` (`live`/`cached`/`mock`) is set on every call and read
  by `extract.py`/`explain.py` so `usage_report.md` reflects what actually
  answered each request, not a hardcoded provider guess.

## `api.py` — optional HTTP layer for a frontend
Thin Flask wrapper — `GET /api/users`, `GET /api/requests`,
`GET /api/requests/<id>`, `POST /api/requests/evaluate`. Every handler
just calls `main.evaluate_one()`; there's no second copy of the decision
logic. Not part of the graded pipeline — `main.py` alone produces
`output.csv` — this exists only to demo the same engine live.

## Validated against the real dataset
No crashes, output passes every structural invariant
(`evaluation/evaluate.py`'s label-free checks — 0 violations). Scored
against `sample_requests.csv` (25 labeled examples):

| metric | before recurrence fixes | after | change |
|---|---|---|---|
| affordability_status accuracy | 0.68 | 0.68 | 0 |
| recommended_payment_method accuracy | 0.80 | 0.68 | -0.12 |
| earliest_date_for_full_payment accuracy | 0.40 | 0.64 | +0.24 |
| spending_changes_needed accuracy | 0.88 | 0.88 | 0 |
| payment_plan exact match | 0.68 | 0.68 | 0 |
| amount_safe_to_pay MAE | ~421k | ~165k | **-61%** |

Honest read of this table: the *continuous* number (`amount_safe_to_pay`)
got dramatically more accurate, and so did the date that depends most
directly on it. But `recommended_payment_method` — a discrete,
boundary-sensitive classification — got slightly worse on this specific
25-row sample. That's a real, expected trade, not a contradiction: pushing
a number closer to the true value can still land it on the wrong side of
a decision boundary for a handful of borderline rows, even while making
every row's underlying quantity more correct. The MAE improvement is the
more trustworthy signal of the two on a sample this small — a 25-row set
is enough to catch a systematic bias (which is exactly how the original
smoothing bug was found) but too small to fully trust every last flipped
categorical call as meaningful signal rather than noise. Worth revisiting
with more labeled examples if any become available.

Earlier finding, from before this session (kept for context): wiring in
real LLM extraction barely moved these numbers versus a fully-mocked run.
That result still stands — the gains this session came entirely from the
deterministic engine, confirming the original diagnosis that the
remaining gap was there, not in missing LLM signal.

## Running
```
pip install -r requirements.txt
export GEMINI_API_KEY="..."          # optional — falls back to a mock without it
python main.py --dataset ../dataset --out ../dataset/output.csv
python main.py --dataset ../dataset --requests-file sample_requests.csv --out /tmp/sample_output.csv
python evaluation/evaluate.py ../dataset --samples /tmp/sample_output.csv
```
`main.py` writes `output.csv` row-by-row as it computes each decision
(flushed immediately) and prints `[12/250] request_12 -> ...` per row, so
a long throttled run is never silent.

## Known TODOs, in priority order
1. Dig into the remaining `recommended_payment_method` mismatches
   (request_06, request_11, request_19 in `sample_requests.csv` are good
   starting points — see the plan_selector rescue/ranking logic) now that
   the underlying amounts feeding into that decision are far more
   accurate than before.
2. request_05, request_08, request_13 were wrong even *before* this
   session's fixes and remain unexplained — worth their own row-by-row
   trace the same way request_03/09/11 were diagnosed here.
3. Spot-check `partial_payment` and `installments` candidates against
   `max_installment_months` — currently a coarse span check.
4. Add unit tests for `forecast.py` (binary search correctness, month-
   projection edge cases like day 31 landing in February) and
   `recurrence.py` (the three-tier logic, the debit-only smoothing
   filter) — pure functions, cheap to test, and this session found two
   real bugs that a test suite would have caught immediately instead of
   requiring a manual trace.
5. Fill in real per-token pricing in `evaluation/usage_report.md`
   (currently a TODO placeholder for the cost line).
