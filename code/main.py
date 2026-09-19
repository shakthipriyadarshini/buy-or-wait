"""
Orchestrator, rebuilt against the ACTUAL dataset headers (verified from the
uploaded dataset.zip on 2026-09-12).

Run:  python main.py --dataset dataset/ --out dataset/output.csv
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

from engine.models import Profile, FinancialEvent, PaymentOption, Request
from engine.currency import RateTable
from engine.conflict_resolution import apply_extracted_signals, resolve_and_classify
from engine.forecast import amount_safe_to_pay, earliest_date_for_full_payment
from engine.plan_selector import build_candidates, rank
from engine.format_output import build_decision_row, _fmt_amount
from engine.extract import extract_message_signal, extract_image_amount, METER
from engine.explain import generate_explanation
from engine.message_targeting import candidate_events_for_message
import llm_client


def parse_date(s: str) -> date | None:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pipe_list(s: str) -> list[str]:
    return [p.strip() for p in s.split("|") if p.strip()]


def mock_call_llm(system: str, user: str):
    """Placeholder — swap for a real Anthropic call. Must return
    (raw_json_or_text, input_tokens, output_tokens)."""
    return ('{"event_id": null, "action": "none", "new_amount": null, '
            '"new_date": null, "delay_days": null, "confidence": 0.0}', 0, 0)


def mock_call_vlm(system: str, image_path: str, event_id: str):
    # placeholder positive value so schema validation passes during a dry
    # run without a real provider wired in — replace with an actual VLM call
    return f'{{"event_id": "{event_id}", "amount": 1.0, "confidence": 0.0}}', 0, 0


def evaluate_one(prof: Profile, req: Request, user_events: list[FinancialEvent],
                  options: list[PaymentOption], call_llm=llm_client.call_llm) -> "Decision":
    """
    Single-request decision path — used by the batch loop in run() below
    AND by api.py so the API is never a second implementation of the logic,
    just a thin HTTP wrapper over this same function.
    """
    safe_today = amount_safe_to_pay(prof, user_events, req.request_date, req.requested_amount)
    earliest_full = earliest_date_for_full_payment(prof, user_events, req.request_date, req.requested_amount)

    # Robustness fallback for ad-hoc/API requests that have no matching rows
    # in request_payment_options.csv (batch runs from the dataset always
    # will; a brand-new request typed into a UI won't): synthesize a plain
    # full_payment option so full-payment is still evaluable.
    if not any(o.payment_method == "full_payment" for o in options) and "full_payment" in prof.payment_methods_user_will_consider:
        options = options + [PaymentOption(
            payment_option_id=f"synthetic_full_{req.request_id}", request_id=req.request_id,
            payment_method="full_payment", payment_amount=req.requested_amount,
            number_of_payments=1, first_payment_date=req.request_date,
            payment_frequency_days=None, financing_fee=0.0, total_payable_amount=req.requested_amount,
        )]

    candidates = build_candidates(prof, req, user_events, options, safe_today, earliest_full)
    best = rank(candidates, req)

    facts = {"currency": prof.home_currency, "current_available_balance": prof.current_available_balance,
             "minimum_balance_to_keep": prof.minimum_balance_to_keep,
             "safe_today": safe_today, "earliest_full": str(earliest_full),
             "requested_amount": req.requested_amount}
    decision_stub = build_decision_row(req.request_id, safe_today, best, earliest_full, req.request_date, "")
    explanation = generate_explanation(decision_stub, facts, call_llm)
    return build_decision_row(req.request_id, safe_today, best, earliest_full, req.request_date, explanation)


def run(dataset_dir: Path, out_path: Path, requests_file: str = "requests.csv"):
    requests_raw = load_csv(dataset_dir / requests_file)
    profiles_raw = load_csv(dataset_dir / "financial_profiles.csv")
    events_raw = load_csv(dataset_dir / "financial_events.csv")
    rates_raw = load_csv(dataset_dir / "exchange_rates.csv")
    options_raw = load_csv(dataset_dir / "request_payment_options.csv")
    messages_raw = load_csv(dataset_dir / "messages.csv")
    images_raw = load_csv(dataset_dir / "images.csv")

    rates = RateTable([
        {"from_currency": r["from_currency"], "to_currency": r["to_currency"],
         "rate_date": parse_date(r["rate_date"]), "rate": r["rate"]}
        for r in rates_raw
    ])

    profiles: dict[str, Profile] = {}
    for r in profiles_raw:
        profiles[r["user_id"]] = Profile(
            user_id=r["user_id"],
            home_currency=r["home_currency"],
            current_available_balance=float(r["current_available_balance"]),
            minimum_balance_to_keep=float(r["minimum_balance_to_keep"]),
            financial_priorities=pipe_list(r.get("financial_priorities", "")),
            expense_categories_to_protect=set(pipe_list(r.get("expense_categories_to_protect", ""))),
            expense_categories_user_is_willing_to_reduce=set(pipe_list(r.get("expense_categories_user_is_willing_to_reduce", ""))),
            expense_categories_user_is_willing_to_stop=set(pipe_list(r.get("expense_categories_user_is_willing_to_stop", ""))),
            payment_methods_user_will_consider=set(pipe_list(r.get("payment_methods_user_will_consider", ""))),
            max_installment_months=int(r["max_installment_months"]) if r.get("max_installment_months") else None,
        )

    # --- events: build objects, fill blank amounts from linked images, convert currency ---
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
            event_date=parse_date(r["event_date"]),
            settlement_date=parse_date(r.get("settlement_date", "")),
            status=r["status"], linked_event_id=r.get("linked_event_id") or None,
            flexibility=r.get("flexibility", "fixed"),
            minimum_allowed_amount=float(r["minimum_allowed_amount"]) if r.get("minimum_allowed_amount") else None,
        )
        if amount is None:
            img = images_by_event.get(ev.event_id)
            if img:
                img_path = dataset_dir / "media" / "images" / f"{img['image_id']}.png"
                extraction = extract_image_amount(str(img_path), ev.event_id, llm_client.call_vlm)
                if extraction.confidence > 0 and extraction.amount > 0:
                    ev.amount = extraction.amount
                else:
                    # Receipt amount could not be read (no vision model
                    # available, or the model wasn't confident). Mark the
                    # event unusable rather than letting a placeholder
                    # amount enter the 90-day forecast as if it were fact.
                    ev.status = "unresolved"
        events.append(ev)

    # currency-normalize every event into the owning user's home_currency
    for ev in events:
        prof = profiles[ev.user_id]
        ev.amount = rates.convert(ev.amount, ev.currency, prof.home_currency, ev.event_date)
        ev.currency = prof.home_currency

    events_by_user: dict[str, list[FinancialEvent]] = defaultdict(list)
    for ev in events:
        events_by_user[ev.user_id].append(ev)
    events_by_id = {ev.event_id: ev for ev in events}

    # --- messages: resolve target event (direct link, or keyword-matched candidates), extract signal ---
    signals = []
    for m in messages_raw:
        uid = m["user_id"]
        related = m.get("related_event_id")
        if related and related in events_by_id:
            candidates = [events_by_id[related]]
        else:
            candidates = candidate_events_for_message(m["message_text"], uid, events_by_user)
        if not candidates:
            continue
        cand_payload = [
            {"event_id": c.event_id, "category": c.category, "description": c.description,
             "amount": c.amount, "event_date": c.event_date.isoformat()}
            for c in candidates
        ]
        sig = extract_message_signal(m["message_text"], cand_payload, llm_client.call_llm)
        valid_ids = {c["event_id"] for c in cand_payload}
        if sig.action == "none" or sig.event_id not in valid_ids:
            continue
        signals.append({
            "event_id": sig.event_id,
            "action": sig.action,
            "value": sig.new_amount if sig.action == "amend_amount"
                     else sig.new_date if sig.action in ("amend_date", "delay") else None,
            "origin_rank": _epoch(m.get("sent_at", "")),
        })

    events = apply_extracted_signals(events, signals)
    events = resolve_and_classify(events, profiles)

    events_by_user = defaultdict(list)
    for ev in events:
        events_by_user[ev.user_id].append(ev)

    options_by_request: dict[str, list[PaymentOption]] = defaultdict(list)
    for r in options_raw:
        options_by_request[r["request_id"]].append(PaymentOption(
            payment_option_id=r["payment_option_id"], request_id=r["request_id"],
            payment_method=r["payment_method"],
            payment_amount=float(r["payment_amount"]),
            number_of_payments=int(r["number_of_payments"]),
            first_payment_date=parse_date(r["first_payment_date"]),
            payment_frequency_days=int(r["payment_frequency_days"]) if r.get("payment_frequency_days") else None,
            financing_fee=float(r.get("financing_fee", 0) or 0),
            total_payable_amount=float(r["total_payable_amount"]),
        ))

    total = len(requests_raw)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["request_id", "amount_safe_to_pay", "affordability_status",
                          "recommended_payment_method", "payment_plan",
                          "earliest_date_for_full_payment", "spending_changes_needed",
                          "decision_explanation"])
        f.flush()

        rows_out = []
        for i, r in enumerate(requests_raw, start=1):
            req = Request(
                request_id=r["request_id"], user_id=r["user_id"],
                request_date=parse_date(r["request_date"]), request_type=r["request_type"],
                requested_amount=float(r["requested_amount"]),
                desired_completion_date=parse_date(r["desired_completion_date"]),
                allows_partial_payment=r.get("allows_partial_payment", "false").lower() == "true",
                request_text=r.get("request_text", ""),
            )
            prof = profiles[req.user_id]
            user_events = events_by_user.get(req.user_id, [])
            options = options_by_request.get(req.request_id, [])
            decision = evaluate_one(prof, req, user_events, options, llm_client.call_llm)
            rows_out.append(decision)

            # write + flush THIS row immediately, not after the whole batch —
            # so `tail -f output.csv` (or just opening the file) shows
            # progress while a long Gemini-throttled run is still going,
            # instead of an empty file until the very end
            #
            # amount_safe_to_pay is formatted through the same helper
            # payment_plan uses, not written as a raw Python float —
            # otherwise every whole-number amount gets a spurious
            # trailing ".0" (e.g. 25256.0 instead of 25256), which is a
            # pure string-formatting mismatch against gold, not a real
            # numeric error, and was silently costing exact-match rows.
            writer.writerow([decision.request_id, _fmt_amount(decision.amount_safe_to_pay), decision.affordability_status,
                              decision.recommended_payment_method, decision.payment_plan,
                              decision.earliest_date_for_full_payment, decision.spending_changes_needed,
                              decision.decision_explanation])
            f.flush()
            print(f"[{i}/{total}] {decision.request_id} -> {decision.affordability_status} / "
                  f"{decision.recommended_payment_method}", flush=True)

    # Path is relative to THIS FILE, not the dataset directory — dataset_dir
    # can point anywhere (an absolute path, a different drive on Windows,
    # etc.), but usage_report.md must land inside code/evaluation/ specifically,
    # since code.zip only ever contains the code/ folder. Deriving the path
    # from dataset_dir.parent was wrong: it wrote to the repo root's
    # evaluation/ folder instead, which never makes it into the submission zip.
    write_usage_report(Path(__file__).parent / "evaluation" / "usage_report.md", len(rows_out))


def _epoch(sent_at: str) -> int:
    """origin_rank for 'newer record wins' comparisons. sent_at is
    ISO-8601 with a Z suffix; fall back to 0 for blank/malformed values."""
    if not sent_at:
        return 0
    try:
        return int(datetime.strptime(sent_at.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp())
    except ValueError:
        return 0


def write_usage_report(path: Path, num_requests: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = METER.summary()
    lines = ["# Token Usage & Cost Report", "", f"Requests processed: {num_requests}", ""]
    total_in = total_out = total_calls = 0
    for model, s in summary.items():
        lines.append(f"## {model}")
        lines.append(f"- calls: {s['calls']}")
        lines.append(f"- input_tokens: {s['input_tokens']}")
        lines.append(f"- output_tokens: {s['output_tokens']}")
        total_in += s["input_tokens"]; total_out += s["output_tokens"]; total_calls += s["calls"]
    lines += ["", "## Totals", f"- total_calls: {total_calls}",
              f"- total_input_tokens: {total_in}", f"- total_output_tokens: {total_out}",
              f"- avg_tokens_per_request: {(total_in+total_out)/max(num_requests,1):.1f}",
              "- estimated_total_cost_usd: TODO (fill in from your provider's per-token pricing)"]
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("dataset"))
    ap.add_argument("--out", type=Path, default=Path("output.csv"),
                     help="Defaults to a repo-root output.csv, matching the official "
                          "'run python3 code/main.py from the repo root' instructions.")
    ap.add_argument("--requests-file", type=str, default="requests.csv",
                     help="Use 'sample_requests.csv' to score against the labeled examples.")
    args = ap.parse_args()
    run(args.dataset, args.out, args.requests_file)
