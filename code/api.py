"""
Thin HTTP layer over the existing engine. Loads the dataset ONCE at
startup into module-level state, then every request just calls
main.evaluate_one() — this file has no decision logic of its own.

Run:
    python api.py --dataset dataset/ --port 5000

Endpoints:
    GET  /api/health
    GET  /api/requests                       -> list of requests (id, user_id, type, amount, text, dates)
    GET  /api/requests/<request_id>          -> full Decision JSON for an existing request
    POST /api/requests/evaluate               -> ad-hoc Decision for a NEW request (JSON body, see below)

POST /api/requests/evaluate body:
{
  "user_id": "user_01",
  "requested_amount": 5000,
  "request_date": "2026-09-12",
  "desired_completion_date": "2026-10-01",
  "allows_partial_payment": true,
  "request_type": "purchase",
  "request_text": "Can I afford this laptop?"
}
user_id must already exist in financial_profiles.csv — the ad-hoc endpoint
reuses that user's existing balance/events, it doesn't create a new user.
"""
from __future__ import annotations
import argparse
import os
from dataclasses import asdict
from pathlib import Path
from flask import Flask, jsonify, request as flask_request, send_from_directory

try:  # local convenience only — hosting platforms inject real env vars
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import main as engine  # reuses load_csv / parse_date / profiles / events loading from main.py
from engine.models import Request

# In production the Vite build is served by this same Flask process, so the
# whole app is one deployable unit (one port, no separate static host, no
# cross-origin config to get wrong). In local dev the Vite dev server runs
# separately on :5173 and talks to this API over CORS instead — that path
# still works and is unaffected.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

app = Flask(__name__, static_folder=None)

# "*" is a sane default for local dev; set CORS_ORIGINS to your real
# frontend URL in production rather than leaving it open.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")

# Upper bound on an ad-hoc requested_amount, to reject absurd input early.
MAX_REQUEST_AMOUNT = float(os.environ.get("MAX_REQUEST_AMOUNT", "1e12"))

# populated once in load_dataset(), read by every request handler
STATE = {}


def load_dataset(dataset_dir: Path):
    # A web process must bind fast. Preprocessing makes up to 231 extraction
    # calls (215 messages + 16 receipt images); at the free tier's 5 req/min
    # that is ~46 minutes of a deployed server not serving traffic. So the
    # API serves cache hits but never makes live calls during boot. Populate
    # the cache with a batch run (`python code/main.py`) before deploying if
    # you want extraction signals applied here.
    engine.llm_client.set_offline_only(True)
    """Mirrors the loading section of main.run() up through
    events_by_user/options_by_request, without the per-request loop —
    that loop is what api.py replaces with HTTP handlers."""
    requests_raw = engine.load_csv(dataset_dir / "requests.csv")
    profiles_raw = engine.load_csv(dataset_dir / "financial_profiles.csv")
    events_raw = engine.load_csv(dataset_dir / "financial_events.csv")
    rates_raw = engine.load_csv(dataset_dir / "exchange_rates.csv")
    options_raw = engine.load_csv(dataset_dir / "request_payment_options.csv")
    messages_raw = engine.load_csv(dataset_dir / "messages.csv")
    images_raw = engine.load_csv(dataset_dir / "images.csv")

    from engine.currency import RateTable
    from engine.models import Profile, FinancialEvent, PaymentOption
    from engine.conflict_resolution import apply_extracted_signals, resolve_and_classify
    from engine.message_targeting import candidate_events_for_message
    from collections import defaultdict

    rates = RateTable([
        {"from_currency": r["from_currency"], "to_currency": r["to_currency"],
         "rate_date": engine.parse_date(r["rate_date"]), "rate": r["rate"]}
        for r in rates_raw
    ])

    profiles: dict[str, Profile] = {}
    for r in profiles_raw:
        profiles[r["user_id"]] = Profile(
            user_id=r["user_id"], home_currency=r["home_currency"],
            current_available_balance=float(r["current_available_balance"]),
            minimum_balance_to_keep=float(r["minimum_balance_to_keep"]),
            financial_priorities=engine.pipe_list(r.get("financial_priorities", "")),
            expense_categories_to_protect=set(engine.pipe_list(r.get("expense_categories_to_protect", ""))),
            expense_categories_user_is_willing_to_reduce=set(engine.pipe_list(r.get("expense_categories_user_is_willing_to_reduce", ""))),
            expense_categories_user_is_willing_to_stop=set(engine.pipe_list(r.get("expense_categories_user_is_willing_to_stop", ""))),
            payment_methods_user_will_consider=set(engine.pipe_list(r.get("payment_methods_user_will_consider", ""))),
            max_installment_months=int(r["max_installment_months"]) if r.get("max_installment_months") else None,
        )

    images_by_event = {i["related_event_id"]: i for i in images_raw if i.get("related_event_id")}
    events: list[FinancialEvent] = []
    for r in events_raw:
        uid = r["user_id"]
        prof = profiles[uid]
        amt_raw = r.get("amount", "")
        amount = float(amt_raw) if amt_raw not in ("", None) else None
        ev = FinancialEvent(
            event_id=r["event_id"], user_id=uid, event_type=r["event_type"],
            description=r.get("description", ""), category=r["category"],
            direction=r["direction"], amount=amount if amount is not None else 0.0,
            currency=r.get("currency", prof.home_currency),
            event_date=engine.parse_date(r["event_date"]),
            settlement_date=engine.parse_date(r.get("settlement_date", "")),
            status=r["status"], linked_event_id=r.get("linked_event_id") or None,
            flexibility=r.get("flexibility", "fixed"),
            minimum_allowed_amount=float(r["minimum_allowed_amount"]) if r.get("minimum_allowed_amount") else None,
        )
        if amount is None:
            img = images_by_event.get(ev.event_id)
            if img:
                img_path = dataset_dir / "media" / "images" / f"{img['image_id']}.png"
                extraction = engine.extract_image_amount(str(img_path), ev.event_id, engine.llm_client.call_vlm)
                if extraction.confidence > 0 and extraction.amount > 0:
                    ev.amount = extraction.amount
                else:
                    # Receipt amount could not be read (no vision model
                    # available, or the model wasn't confident). Mark the
                    # event unusable rather than letting a placeholder
                    # amount enter the 90-day forecast as if it were fact.
                    ev.status = "unresolved"
        events.append(ev)

    for ev in events:
        prof = profiles[ev.user_id]
        ev.amount = rates.convert(ev.amount, ev.currency, prof.home_currency, ev.event_date)
        ev.currency = prof.home_currency

    events_by_id = {ev.event_id: ev for ev in events}
    events_by_user_tmp: dict[str, list[FinancialEvent]] = defaultdict(list)
    for ev in events:
        events_by_user_tmp[ev.user_id].append(ev)

    signals = []
    for m in messages_raw:
        uid = m["user_id"]
        related = m.get("related_event_id")
        candidates = [events_by_id[related]] if related and related in events_by_id \
            else candidate_events_for_message(m["message_text"], uid, events_by_user_tmp)
        if not candidates:
            continue
        cand_payload = [
            {"event_id": c.event_id, "category": c.category, "description": c.description,
             "amount": c.amount, "event_date": c.event_date.isoformat()}
            for c in candidates
        ]
        sig = engine.extract_message_signal(m["message_text"], cand_payload, engine.llm_client.call_llm)
        valid_ids = {c["event_id"] for c in cand_payload}
        if sig.action == "none" or sig.event_id not in valid_ids:
            continue
        signals.append({
            "event_id": sig.event_id, "action": sig.action,
            "value": sig.new_amount if sig.action == "amend_amount"
                     else sig.new_date if sig.action in ("amend_date", "delay") else None,
            "origin_rank": engine._epoch(m.get("sent_at", "")),
        })

    events = apply_extracted_signals(events, signals)
    events = resolve_and_classify(events, profiles)

    events_by_user: dict[str, list[FinancialEvent]] = defaultdict(list)
    for ev in events:
        events_by_user[ev.user_id].append(ev)

    options_by_request: dict[str, list[PaymentOption]] = defaultdict(list)
    for r in options_raw:
        options_by_request[r["request_id"]].append(PaymentOption(
            payment_option_id=r["payment_option_id"], request_id=r["request_id"],
            payment_method=r["payment_method"], payment_amount=float(r["payment_amount"]),
            number_of_payments=int(r["number_of_payments"]),
            first_payment_date=engine.parse_date(r["first_payment_date"]),
            payment_frequency_days=int(r["payment_frequency_days"]) if r.get("payment_frequency_days") else None,
            financing_fee=float(r.get("financing_fee", 0) or 0),
            total_payable_amount=float(r["total_payable_amount"]),
        ))

    STATE["profiles"] = profiles
    STATE["events_by_user"] = events_by_user
    STATE["options_by_request"] = options_by_request
    STATE["requests_raw"] = requests_raw
    STATE["requests_by_id"] = {r["request_id"]: r for r in requests_raw}


@app.after_request
def add_cors_headers(resp):
    origin = flask_request.headers.get("Origin", "")
    if CORS_ORIGINS == "*":
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in {o.strip() for o in CORS_ORIGINS.split(",")}:
        # echo back only an origin that's actually on the allowlist, rather
        # than reflecting whatever asked
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/health")
def health():
    return jsonify(
        status="ok",
        requests_loaded=len(STATE.get("requests_raw", [])),
        frontend_bundled=FRONTEND_DIST.is_dir(),
    )


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path: str):
    """Serves the built Vite bundle when one exists. Any unknown path falls
    through to index.html so client-side routing works on a hard refresh.
    /api/* never reaches here — those routes are registered above and match
    first."""
    if not FRONTEND_DIST.is_dir():
        return jsonify(
            error="Frontend not built.",
            hint="Run `npm ci && npm run build` in frontend/, or use the API directly at /api/health.",
        ), 404
    target = FRONTEND_DIST / path
    if path and target.is_file():
        return send_from_directory(FRONTEND_DIST, path)
    return send_from_directory(FRONTEND_DIST, "index.html")


@app.route("/api/requests")
def list_requests():
    rows = STATE["requests_raw"]
    return jsonify([
        {"request_id": r["request_id"], "user_id": r["user_id"], "request_type": r["request_type"],
         "requested_amount": r["requested_amount"], "request_date": r["request_date"],
         "desired_completion_date": r["desired_completion_date"], "request_text": r["request_text"]}
        for r in rows
    ])


def _parse_plan_payments(plan_str: str):
    """"2026-09-16:500|2026-10-16:500" -> [(date, 500.0), ...]"""
    if not plan_str or plan_str == "none":
        return []
    out = []
    for chunk in plan_str.split("|"):
        try:
            d, amt = chunk.split(":")
            out.append((engine.parse_date(d), float(amt)))
        except (ValueError, TypeError):
            continue
    return out


def _forecast_series(prof, user_events, request_date, payments=None, max_points=90) -> list[dict]:
    """The real day-by-day 90-day balance walk the engine already computes
    internally — previously it never left the backend, so the UI charted
    placeholder data. `payments` layers a candidate plan on top, which is
    what makes a with-purchase vs without-purchase comparison possible."""
    from engine.recurrence import detect_and_project
    from engine.forecast import daily_deltas, simulate_balance, HORIZON_DAYS

    sims = detect_and_project(user_events, request_date, HORIZON_DAYS)
    deltas = daily_deltas(sims, request_date, HORIZON_DAYS)
    balances = simulate_balance(prof.current_available_balance, deltas,
                                 request_date, HORIZON_DAYS, extra_payments=payments)
    ordered = sorted(balances.items())
    # Down-sample evenly if a caller wants fewer points; charts don't need 91.
    step = max(1, len(ordered) // max_points)
    return [{"date": d.isoformat(), "balance": round(b, 2)}
            for i, (d, b) in enumerate(ordered) if i % step == 0 or i == len(ordered) - 1]


def _profile_snapshot(prof) -> dict:
    return {
        "user_id": prof.user_id,
        "home_currency": prof.home_currency,
        "current_available_balance": prof.current_available_balance,
        "minimum_balance_to_keep": prof.minimum_balance_to_keep,
        "payment_methods_user_will_consider": sorted(prof.payment_methods_user_will_consider),
    }


@app.route("/api/users")
def list_users():
    return jsonify([_profile_snapshot(p) for p in STATE["profiles"].values()])


@app.route("/api/requests/<request_id>")
def get_decision(request_id: str):
    r = STATE["requests_by_id"].get(request_id)
    if r is None:
        return jsonify(error=f"unknown request_id: {request_id}"), 404
    req = Request(
        request_id=r["request_id"], user_id=r["user_id"],
        request_date=engine.parse_date(r["request_date"]), request_type=r["request_type"],
        requested_amount=float(r["requested_amount"]),
        desired_completion_date=engine.parse_date(r["desired_completion_date"]),
        allows_partial_payment=r.get("allows_partial_payment", "false").lower() == "true",
        request_text=r.get("request_text", ""),
    )
    prof = STATE["profiles"][req.user_id]
    user_events = STATE["events_by_user"].get(req.user_id, [])
    options = STATE["options_by_request"].get(req.request_id, [])
    decision = engine.evaluate_one(prof, req, user_events, options)
    plan_payments = _parse_plan_payments(decision.payment_plan)
    return jsonify({
        **asdict(decision),
        "profile": _profile_snapshot(prof),
        "forecast_without_purchase": _forecast_series(prof, user_events, req.request_date),
        "forecast_with_purchase": _forecast_series(prof, user_events, req.request_date, payments=plan_payments),
    })


@app.route("/api/requests/evaluate", methods=["POST", "OPTIONS"])
def evaluate_adhoc():
    if flask_request.method == "OPTIONS":
        return "", 204
    try:
        body = flask_request.get_json(force=True)
    except Exception:
        return jsonify(error="request body must be valid JSON"), 400
    if not isinstance(body, dict):
        return jsonify(error="request body must be a JSON object"), 400

    required = ["user_id", "requested_amount", "request_date", "desired_completion_date"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify(error=f"missing required fields: {', '.join(missing)}"), 400
    if body["user_id"] not in STATE["profiles"]:
        return jsonify(error=f"unknown user_id: {body['user_id']}"), 404

    # Validate every user-supplied value explicitly. Previously these were
    # fed straight into float()/parse_date(), so a non-numeric amount or a
    # malformed date surfaced as a 500 rather than a usable error message.
    try:
        amount = float(body["requested_amount"])
    except (TypeError, ValueError):
        return jsonify(error="requested_amount must be a number"), 400
    if not (amount > 0):
        return jsonify(error="requested_amount must be greater than 0"), 400
    if amount > MAX_REQUEST_AMOUNT:
        return jsonify(error=f"requested_amount exceeds the maximum of {MAX_REQUEST_AMOUNT:,.0f}"), 400

    try:
        req_date = engine.parse_date(body["request_date"])
        done_date = engine.parse_date(body["desired_completion_date"])
    except (TypeError, ValueError):
        return jsonify(error="dates must be in YYYY-MM-DD format"), 400
    if req_date is None or done_date is None:
        return jsonify(error="request_date and desired_completion_date are required"), 400
    if done_date < req_date:
        return jsonify(error="desired_completion_date must be on or after request_date"), 400

    allows_partial = body.get("allows_partial_payment", False)
    if not isinstance(allows_partial, bool):
        return jsonify(error="allows_partial_payment must be true or false"), 400

    req = Request(
        request_id=body.get("request_id", "adhoc"), user_id=body["user_id"],
        request_date=req_date,
        request_type=str(body.get("request_type", "purchase"))[:64],
        requested_amount=amount,
        desired_completion_date=done_date,
        allows_partial_payment=allows_partial,
        request_text=str(body.get("request_text", ""))[:1000],
    )
    prof = STATE["profiles"][req.user_id]
    user_events = STATE["events_by_user"].get(req.user_id, [])
    decision = engine.evaluate_one(prof, req, user_events, options=[])
    plan_payments = _parse_plan_payments(decision.payment_plan)
    return jsonify({
        **asdict(decision),
        "profile": _profile_snapshot(prof),
        "forecast_without_purchase": _forecast_series(prof, user_events, req.request_date),
        "forecast_with_purchase": _forecast_series(prof, user_events, req.request_date, payments=plan_payments),
    })


def _default_dataset_dir() -> Path:
    """Resolved relative to the repo root (this file's parent's parent), not
    the process's working directory — a WSGI server may be started from
    anywhere, and a relative "dataset" would silently resolve wrong."""
    env_dir = os.environ.get("DATASET_DIR")
    if env_dir:
        p = Path(env_dir)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "dataset"


def create_app(dataset_dir: Path | None = None) -> Flask:
    """WSGI entrypoint. Gunicorn and friends import the module and call this
    rather than executing __main__, so the dataset load has to happen here
    too — not only in the __main__ block below."""
    load_dataset(dataset_dir or _default_dataset_dir())
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=None,
                     help="Defaults to $DATASET_DIR, else <repo>/dataset.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")),
                     help="Defaults to $PORT (most hosts inject this), else 5000.")
    args = ap.parse_args()
    load_dataset(args.dataset or _default_dataset_dir())
    app.run(host="0.0.0.0", port=args.port, debug=False)
