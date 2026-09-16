"""
Two-tier evaluation:

1. score_against_samples(): field-level accuracy vs dataset/sample_requests.csv
   (the only rows with visible ground truth). Use this to tune the engine.

2. check_invariants(): label-free structural/business-rule checks that run
   against the FULL output.csv (including the hidden-label rows). These
   catch the failure modes the hidden grader will punish even though you
   never see its answer key — run this as a CI gate before every submission.
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_plan(s: str) -> list[tuple[str, float]]:
    if s == "none" or not s:
        return []
    out = []
    for chunk in s.split("|"):
        d, amt = chunk.split(":")
        out.append((d, float(amt)))
    return out


def score_against_samples(output_path: Path, sample_path: Path) -> dict:
    preds = {r["request_id"]: r for r in load(output_path)}
    golds = load(sample_path)
    n = len(golds)
    exact_fields = ["affordability_status", "recommended_payment_method",
                    "earliest_date_for_full_payment", "spending_changes_needed"]
    scores = {f: 0 for f in exact_fields}
    amount_abs_err = []
    plan_matches = 0
    missing = 0

    for g in golds:
        rid = g["request_id"]
        p = preds.get(rid)
        if p is None:
            missing += 1
            continue
        for f in exact_fields:
            if p.get(f, "").strip() == g.get(f, "").strip():
                scores[f] += 1
        try:
            amount_abs_err.append(abs(float(p["amount_safe_to_pay"]) - float(g["amount_safe_to_pay"])))
        except (ValueError, KeyError):
            amount_abs_err.append(float("inf"))
        if parse_plan(p.get("payment_plan", "")) == parse_plan(g.get("payment_plan", "")):
            plan_matches += 1

    result = {f"{f}_accuracy": scores[f] / n for f in exact_fields}
    result["payment_plan_exact_match"] = plan_matches / n
    result["amount_mae"] = sum(amount_abs_err) / len(amount_abs_err) if amount_abs_err else None
    result["missing_predictions"] = missing
    result["n_samples"] = n
    return result


def check_invariants(output_path: Path, requests_path: Path, options_path: Path) -> list[str]:
    """Returns a list of violation strings. Empty list == clean."""
    rows = load(output_path)
    req_by_id = {r["request_id"]: r for r in load(requests_path)}
    options = load(options_path)
    options_by_id = {o["payment_option_id"]: o for o in options}
    violations = []

    for r in rows:
        rid = r["request_id"]
        req = req_by_id.get(rid)
        if req is None:
            violations.append(f"{rid}: unknown request_id in output")
            continue
        try:
            safe = float(r["amount_safe_to_pay"])
            requested = float(req["requested_amount"])
        except ValueError:
            violations.append(f"{rid}: non-numeric amount")
            continue
        if not (0 <= safe <= requested):
            violations.append(f"{rid}: amount_safe_to_pay {safe} outside [0, {requested}]")

        status = r["affordability_status"]
        method = r["recommended_payment_method"]
        plan = parse_plan(r["payment_plan"])

        if status == "affordable_now" and r["earliest_date_for_full_payment"] != req["request_date"]:
            violations.append(f"{rid}: affordable_now but earliest_date != request_date")

        if method == "partial_payment":
            if status != "affordable_with_plan":
                violations.append(f"{rid}: partial_payment must pair with affordable_with_plan")
            if len(plan) != 2:
                violations.append(f"{rid}: partial_payment plan must have exactly 2 payments")
            elif round(plan[0][1] + plan[1][1], 2) != round(requested, 2):
                violations.append(f"{rid}: partial_payment payments don't sum to requested_amount")

        if method == "installments":
            amounts = [a for _, a in plan]
            matched = any(
                o["payment_method"] == "installments" and
                len(plan) == int(o["number_of_payments"]) and
                all(abs(a - float(o["payment_amount"])) < 0.01 for a in amounts)
                for o in options if o["request_id"] == rid
            )
            if not matched:
                violations.append(f"{rid}: installments plan doesn't match any supplied payment_option")

        changes = r["spending_changes_needed"]
        if changes and changes != "none":
            parts = changes.split("|")
            if len(parts) > 3:
                violations.append(f"{rid}: more than 3 spending changes")
            stop_ids = {p.split(":")[1] for p in parts if p.startswith("stop:")}
            reduce_ids = {p.split(":")[1] for p in parts if p.startswith("reduce_to:")}
            if stop_ids & reduce_ids:
                violations.append(f"{rid}: same event both stopped and reduced")

    return violations


if __name__ == "__main__":
    # Usage:
    #   python evaluate.py dataset/                        -> invariants only, vs requests.csv
    #   python evaluate.py dataset/ --samples out.csv       -> score out.csv vs sample_requests.csv
    dataset = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dataset")
    requests_path = dataset / "requests.csv"
    out = dataset / "output.csv"

    if "--samples" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--samples") + 1])
        requests_path = dataset / "sample_requests.csv"
        sample = dataset / "sample_requests.csv"
        print("=== Field-level accuracy vs sample_requests.csv ===")
        for k, v in score_against_samples(out, sample).items():
            print(f"  {k}: {v}")

    print("\n=== Invariant check (all rows) ===")
    violations = check_invariants(out, requests_path, dataset / "request_payment_options.csv")
    if not violations:
        print("  OK — no violations")
    else:
        for v in violations:
            print(f"  VIOLATION: {v}")
        sys.exit(1)
