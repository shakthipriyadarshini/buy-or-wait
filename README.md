# Buy or Wait? — AI financial affordability agent

Decides whether a user can safely afford a requested expense, by simulating
their cash flow day-by-day over a 90-day horizon. Answers *how much* is safe
to pay today, *when* the full amount becomes safe, *which* payment method to
use, and *what* spending would need to change.

Built for the HackerRank "Orchestrate" challenge; now maintained as a
portfolio project.

---

## The core design decision

**The LLM never does arithmetic.** It's used at exactly two narrow seams:

1. **Extraction** — reading unstructured messages and receipt images into a
   *validated, closed-schema* signal against one specific known event
   (`cancel` / `amend_amount` / `amend_date` / `delay` / `confirm`). It can
   never invent an event or change the schema.
2. **Explanation** — writing a one-sentence caption for a decision that has
   *already been computed*. It is explicitly told not to recalculate.

Everything that has to be numerically correct — currency normalization,
recurrence detection, the 90-day balance walk, `amount_safe_to_pay`,
`earliest_date_for_full_payment`, plan ranking — is deterministic Python.

This isn't LLM-skepticism for its own sake. "Can this person stay above
their minimum balance through day 90" is arithmetic, and arithmetic from a
language model is a guess with good grammar. Keeping it deterministic also
makes every number traceable to a specific line of reasoning, which is what
made the debugging work below possible at all.

---

## Architecture

```
CSVs ──► load + currency-normalize
          │
          ▼
    recurrence detection  (3 tiers — see below)
          │
          ▼
    message/image extraction ──► conflict resolution
          │                       (explicit > newer > settled > safer)
          ▼
    90-day balance simulation   ──► amount_safe_to_pay
          │                          earliest_date_for_full_payment
          ▼
    candidate plans ──► safety filter ──► spending-change rescue
          │                                (bounded search, ≤3 changes)
          ▼
    6-level tie-break ranking ──► explanation ──► output.csv
```

| File | Responsibility |
|---|---|
| `code/main.py` | Orchestrator; batch run → `output.csv` |
| `code/engine/recurrence.py` | Detects recurring series from raw transaction history |
| `code/engine/forecast.py` | 90-day simulation; binary-searches the max safe payment |
| `code/engine/plan_selector.py` | Builds candidate plans, ranks by the spec's tie-break |
| `code/engine/spending_changes.py` | Bounded search for a stop/reduce combo that rescues a plan |
| `code/engine/conflict_resolution.py` | Applies extracted signals; classifies flexibility |
| `code/engine/extract.py` | Closed-schema LLM extraction (untrusted input) |
| `code/llm_client.py` | Gemini backend: rate limiting, caching, hard timeouts |
| `code/api.py` | Flask API + serves the built frontend |
| `code/evaluation/evaluate.py` | Field scoring + label-free invariant checks |

### Recurrence detection, in three tiers

The dataset has **no** `is_recurring` field — just 25k+ raw transactions. So
recurrence is *detected*, per category:

1. **Per-(category, description)** — one named bill (rent, a subscription).
   Projects at the literal last observed amount, so a message-driven
   amendment propagates exactly.
2. **Pooled per-category** — catches real patterns that rotate between
   merchants (groceries at 7 different stores, but reliably every ~10 days)
   that no single merchant's history would ever reach 3 points for.
3. **Smoothed average rate** — for genuinely irregular spending (dining,
   ad-hoc transport). Debit-only, and never offered as a spending-change
   target since it's a pooled estimate, not one named commitment.

Monthly series project on the same **calendar day** each month, not a fixed
30-day step, which drifts over a 90-day window.

---

## Results

Scored against the 25 labeled examples in `dataset/sample_requests.csv`:

| metric | before recurrence work | after | change |
|---|---|---|---|
| `amount_safe_to_pay` MAE | ~421k | ~165k | **−61%** |
| `earliest_date_for_full_payment` accuracy | 0.40 | 0.64 | **+0.24** |
| `affordability_status` accuracy | 0.68 | 0.68 | 0 |
| `recommended_payment_method` accuracy | 0.80 | 0.68 | −0.12 |
| `spending_changes_needed` accuracy | 0.88 | 0.88 | 0 |
| structural invariant violations (all 250 rows) | 0 | 0 | — |

**Reading this honestly:** the continuous quantity got dramatically more
accurate, and so did the date that depends on it. But
`recommended_payment_method` — a discrete, boundary-sensitive call — got
slightly worse on this 25-row sample. That's a real trade, not a
contradiction: moving a number closer to truth can still push a handful of
borderline rows across a decision boundary. On a sample this small, the MAE
improvement is the more trustworthy signal; 25 rows is enough to expose a
systematic bias but too small to treat every flipped categorical as meaning.

### Two bugs worth naming

**Irregular income projected as expense.** The first version of tier 3
computed smoothed amounts from the unsigned `amount` field and force-applied
a debit sign to *everything* left over — including income. Any user whose
salary missed the periodicity test had their paycheck modeled as a large
recurring expense. Found by tracing one user's forecast line-by-line after a
regression, not by reading the code. The structural invariant checker passed
throughout — it validates shape, not sign.

**An intuition that measurement overruled.** Tier 2 initially projected at
the average of the last 5 occurrences, reasoning that one grocery run's price
is noisier than one named bill's. Measured both ways: averaging helped two
individual rows but made aggregate MAE *worse* (164.6k → 177.1k). Reverted
to the simpler rule on the strength of the measurement.

---

## Quick start

```bash
git clone <your-repo-url> && cd <repo>
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional — without a key it falls back to the committed response cache,
# then to a deterministic mock, and still runs end to end.
cp .env.example .env    # then paste a free key from https://aistudio.google.com/apikey

# Batch run → output.csv
python code/main.py --dataset dataset --out output.csv

# Score against the labeled examples
python code/main.py --dataset dataset --requests-file sample_requests.csv --out /tmp/sample.csv
python code/evaluation/evaluate.py dataset --samples /tmp/sample.csv
```

`main.py` writes each row the moment it's computed and prints
`[12/250] request_12 -> affordable_now / full_payment`, so a long
rate-limited run is never silent.

### Running the web app locally

Two terminals:

```bash
# terminal 1 — API on :5000
python code/api.py

# terminal 2 — Vite dev server on :5173, proxies /api to :5000
cd frontend && npm install && npm run dev
```

---

## Deployment

The app is one deployable unit: Flask serves both the API and the built
frontend on a single port.

**Docker (works anywhere):**
```bash
docker build -t buy-or-wait .
docker run -p 5000:5000 -e GEMINI_API_KEY=your_key buy-or-wait
# open http://localhost:5000
```

**Render:** New → Blueprint → point at this repo. `render.yaml` is
preconfigured (free tier, health check on `/api/health`). Set
`GEMINI_API_KEY` in the dashboard — never in the repo.

**Railway / Fly.io / Cloud Run:** point them at the `Dockerfile`; no other
config needed. Set env vars per `.env.example`.

**Manual / VPS:**
```bash
cd frontend && npm ci && npm run build && cd ..
gunicorn --chdir code wsgi:app --bind 0.0.0.0:5000 --workers 1 --threads 2
```

> **Why one worker?** Each worker parses the full dataset into memory, and
> the Gemini rate limiter is a per-process in-memory window — multiple
> workers would each keep their own counter and collectively blow past the
> free-tier quota. Scale with threads, not workers, unless you move the
> limiter to shared storage.

### Environment variables

| var | default | purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(unset)* | Free key. Unset → cache, then mock. |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Flash-Lite has a higher free quota. |
| `GEMINI_RPM_LIMIT` | `5` | Self-throttle rate. Raise to your real quota. |
| `GEMINI_TIMEOUT_SECS` | `30` | Hard deadline, enforced outside the SDK. |
| `DATASET_DIR` | `dataset` | Where the CSVs live. |
| `PORT` | `5000` | Most hosts inject this. |
| `CORS_ORIGINS` | `*` | Set to your frontend URL in production. |

---

## Notes on the LLM client

Built through several rounds of hitting free-tier limits for real:

- **Proactive rate limiting** — throttles *before* exceeding budget, rather
  than firing and reacting to 429s (which just burns the retry budget).
- **Hard external timeout** — the `google-genai` SDK's own `timeout` is
  unreliable (a documented upstream issue: requests can stall indefinitely).
  Every call runs in a worker thread with a deadline enforced via
  `Future.result(timeout=...)`. A real run hung for an hour before this.
- **On-disk response cache** (`code/.llm_cache.json`, **not committed**) —
  keyed by request hash; avoids re-spending quota on repeat runs. It's a
  generated artifact stored as one ~139KB JSON line, which git would handle
  badly (whole-blob rewrite and an unreadable diff on every change).
- **Daily-quota fast-fail** — RPM and RPD both surface as 429, but only RPM
  heals in under a minute. If every retry for one call still 429s, the run
  stops paying retry cost on every remaining call.

### Running without an API key

The system is fully functional with no key, no quota and no cache:

- `decision_explanation` comes from `engine/explain.deterministic_explanation()`,
  which composes a sentence from the already-computed numbers. Nothing is
  derived or recalculated there — it restates what the engine decided.
- Message/image extraction returns a no-op signal, so no message-driven
  amendments are applied.

### Reproducing the results

The metrics table above was produced **with** extraction signals available.
Without them, the continuous metric is unchanged (`amount_safe_to_pay` MAE
stays at ~165k — the engine's arithmetic doesn't depend on the LLM at all),
but a few borderline categorical calls shift:

| metric | with extraction | no key / no cache |
|---|---|---|
| `amount_safe_to_pay` MAE | ~165k | ~165k (identical) |
| `affordability_status` accuracy | 0.68 | 0.64 |
| `recommended_payment_method` accuracy | 0.68 | 0.64 |

To reproduce the first column exactly, set `GEMINI_API_KEY` and run once
(the cache is rebuilt as it goes). This is a genuine limitation of not
committing the cache, and the honest trade for keeping the repo clean.

---

## Deployment behaviour worth knowing

**The API never makes live LLM calls at startup.** Preprocessing the dataset
involves up to 231 extraction calls (215 messages + 16 receipt images); at
the free tier's 5 req/min that would be ~46 minutes of a deployed server
refusing traffic before it binds. `api.py` therefore sets
`llm_client.set_offline_only(True)` — it serves cache hits but never blocks
on the network. Measured cold start: **under 1 second**.

To have extraction signals applied in the deployed app, run the batch
pipeline once with a key (`python code/main.py`) to populate
`code/.llm_cache.json` before building the image.

**Unresolvable receipts are excluded, not guessed.** If a receipt image
can't be read (no vision model, or low confidence), the event is marked
`unresolved` and dropped from the forecast. An earlier version returned a
placeholder amount of `1.0` to satisfy the schema validator — which meant an
unreadable receipt silently entered the forecast as a real 1-unit
transaction. Fabricating a financial figure to satisfy a validator is worse
than admitting the gap.

## Known limitations

- `recommended_payment_method` regressed slightly on the sample set (see
  Results); the remaining mismatches (`request_06`, `request_11`,
  `request_19`) haven't been traced yet.
- `request_05`, `request_08`, `request_13` were wrong before the recurrence
  work too, and remain unexplained.
- **No authentication or rate limiting.** Anyone who can reach the deployed
  URL can call `/api/requests/evaluate`. Fine for a demo; add both before
  treating it as a real service.
- **`/api/users` exposes every profile in the dataset.** This is synthetic
  HackerRank data that also ships in `dataset/`, so it leaks nothing real —
  but a polished demo would expose 2–3 curated profiles rather than all 275.
- **The frontend uses today's real date** for ad-hoc requests, while the
  dataset's events run to ~Sept 2026. Decisions therefore shift depending on
  when you open the page. A fixed snapshot date would make the demo
  deterministic.
- No unit tests yet. Both bugs above would have been caught instantly by
  tests over `recurrence.py` and `forecast.py`, which are pure functions.
- `max_installment_months` is enforced with a coarse span check.
