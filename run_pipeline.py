"""
STEP 6: Full pipeline + audit trail
Runs generate -> detect -> diagnose -> recover -> validate in one go,
and saves a complete audit trail of every decision made.
"""

import json
import warnings
import pandas as pd
from datetime import datetime

warnings.filterwarnings("ignore", message="Direct use of automatic function calling")

from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies
from diagnose_llm import diagnose_all_anomalies_llm, client, GEMINI_MODEL, GEMINI_MODEL_FALLBACK
from recover import run_full_recovery_pipeline
from validate import load_ground_truth, validate_pipeline


def build_audit_trail(results):
    """
    Convert our results table into a clean, readable audit log -
    one entry per event, showing the full decision chain:
    detected -> diagnosed -> action taken -> outcome.
    """
    audit_entries = []

    for _, row in results.iterrows():
        entry = {
            "timestamp_logged": datetime.now().isoformat(),
            "detected": {
                "issuer": row["issuer"],
                "time_window": str(row["time_window"]),
            },
            "diagnosed": {
                "cause": row["diagnosed_cause"],
                "confidence": row["confidence"],
            },
            "action_taken": row["action_taken"],
            "outcome": {
                "amount_at_risk": row["amount_at_risk"],
                "amount_recovered": row["amount_recovered"],
                "txns_recovered": int(row["num_txns_recovered"]),
                "txns_still_failed": int(row["num_txns_still_failed"]),
            },
        }
        audit_entries.append(entry)

    return audit_entries


def generate_incident_summary(results):
    """
    One additional small LLM call: summarize the whole run in 2-4
    plain-English sentences, the way an analyst would report it to a
    manager. This is genuinely useful (not just a demo flourish) - it
    turns a table of numbers into something a non-technical reviewer
    can act on immediately. Falls back to a simple templated summary
    if the API call fails, so the pipeline never breaks over this.
    """
    total_at_risk = results["amount_at_risk"].sum()
    total_recovered = results["amount_recovered"].sum()
    num_events = len(results)
    num_auto_recovered = (results["amount_recovered"] > 0).sum()
    num_manual = (results["action_taken"] == "manual_flag").sum()

    events_text = "\n".join(
        f"- {row['issuer']} ({row['time_window']}): {row['diagnosed_cause']}, "
        f"action={row['action_taken']}, recovered Rs.{row['amount_recovered']:,.2f} "
        f"of Rs.{row['amount_at_risk']:,.2f}"
        for _, row in results.iterrows()
    )

    prompt = f"""You are reporting to a non-technical payments operations manager.
Summarize this run of an automated payment-recovery agent in 3-4 plain
English sentences - no jargon, no bullet points, just a short narrative
paragraph a manager could read in 10 seconds. Mention the total amount
recovered, the biggest single event, and how many cases were left for
manual/human review and why that's appropriate (not a failure).

Run data:
- {num_events} anomalies detected and diagnosed
- Rs.{total_recovered:,.2f} recovered out of Rs.{total_at_risk:,.2f} at risk
- {num_auto_recovered} events auto-recovered, {num_manual} routed to manual review

Per-event detail:
{events_text}

Respond with ONLY the summary paragraph, nothing else - no headers, no labels."""

    try:
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        except Exception:
            response = client.models.generate_content(model=GEMINI_MODEL_FALLBACK, contents=prompt)
        return response.text.strip()
    except Exception as e:
        # Fallback: simple templated summary, never let this break the pipeline
        recovery_pct = (total_recovered / total_at_risk * 100) if total_at_risk > 0 else 0
        return (
            f"[Auto-generated summary unavailable ({e}) - templated fallback:] "
            f"The agent detected {num_events} anomalies, recovering Rs.{total_recovered:,.2f} "
            f"of Rs.{total_at_risk:,.2f} at risk ({recovery_pct:.1f}%). "
            f"{num_manual} case(s) were routed to manual review rather than automated action."
        )


def print_report(results):
    """Print a clean, human-readable summary report to the console."""
    print(f"\n{'='*70}")
    print("PAYMENT DEGRADATION RECOVERY - RUN REPORT")
    print(f"{'='*70}\n")

    print(f"Anomalies detected & diagnosed: {len(results)}\n")

    for _, row in results.iterrows():
        print(f"[{row['issuer']} | {row['time_window']}]")
        print(f"  Diagnosed cause : {row['diagnosed_cause']} (confidence: {row['confidence']})")
        print(f"  Action taken    : {row['action_taken']}")
        print(f"  Amount at risk  : Rs.{row['amount_at_risk']:,.2f}")
        print(f"  Amount recovered: Rs.{row['amount_recovered']:,.2f}")
        print(f"  Txns recovered  : {row['num_txns_recovered']} / "
              f"{row['num_txns_recovered'] + row['num_txns_still_failed']}")
        print()

    total_at_risk = results["amount_at_risk"].sum()
    total_recovered = results["amount_recovered"].sum()
    recovery_pct = (total_recovered / total_at_risk * 100) if total_at_risk > 0 else 0

    print(f"{'='*70}")
    print(f"TOTAL AMOUNT AT RISK:    Rs.{total_at_risk:,.2f}")
    print(f"TOTAL AMOUNT RECOVERED:  Rs.{total_recovered:,.2f}")
    print(f"OVERALL RECOVERY RATE:   {recovery_pct:.1f}%")
    print(f"{'='*70}")


if __name__ == "__main__":
    # Back up any existing good results BEFORE this run, so a failed/quota-
    # exhausted run never silently destroys a previous working result
    import shutil
    import os as _os
    if _os.path.exists("data/pipeline_results.csv"):
        _os.makedirs("data/backup", exist_ok=True)
        for fname in ["pipeline_results.csv", "audit_trail.json",
                      "incident_summary.txt", "validation_metrics.json"]:
            src = f"data/{fname}"
            if _os.path.exists(src):
                shutil.copy(src, f"data/backup/{fname}")
        print("Backed up previous results to data/backup/ before this run.\n")

    # Run the full pipeline
    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)
    print(f"Diagnosing {len(anomalies)} flagged anomalies using Gemini...\n")
    diagnoses = diagnose_all_anomalies_llm(df, anomalies)
    results = run_full_recovery_pipeline(diagnoses)

    # Print the human-readable report
    print_report(results)

    # Generate and print the plain-English incident summary (1 more LLM call)
    print("\nGenerating incident summary...\n")
    summary = generate_incident_summary(results)
    print(f"{'='*70}")
    print("INCIDENT SUMMARY (for a non-technical reviewer)")
    print(f"{'='*70}")
    print(summary)
    print(f"{'='*70}")

    with open("data/incident_summary.txt", "w") as f:
        f.write(summary)
    print("\nSummary saved to data/incident_summary.txt")

    # Save the audit trail
    audit_trail = build_audit_trail(results)
    with open("data/audit_trail.json", "w") as f:
        json.dump(audit_trail, f, indent=2, default=str)
    print(f"\nAudit trail saved to data/audit_trail.json ({len(audit_trail)} entries)")

    # Save the results table too, for reference
    results.to_csv("data/pipeline_results.csv", index=False)
    print("Results saved to data/pipeline_results.csv")

    # Run validation against ground truth
    print("\n")
    ground_truth = load_ground_truth()
    validation_metrics = validate_pipeline(ground_truth, results)

    with open("data/validation_metrics.json", "w") as f:
        json.dump(validation_metrics, f, indent=2)
    print("\nValidation metrics saved to data/validation_metrics.json")