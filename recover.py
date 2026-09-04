"""
STEP 4: Recovery action execution + outcome simulation
Uses the recovery action RECOMMENDED BY THE LLM (from diagnose_llm.py)
rather than a fixed lookup table - the agent decides the action itself,
based on cause, confidence, AND amount at risk. A rule-based fallback
mapping is kept only as a safety net for missing/invalid LLM output.
"""

import random
import pandas as pd
from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies
from diagnose_llm import diagnose_all_anomalies_llm

random.seed(42)  # reproducible simulation results

# Fallback only - used if a row somehow has no valid recommended_action
FALLBACK_CAUSE_TO_ACTION = {
    "issuer_outage": "retry_later",
    "card_expiry_cluster": "prompt_update",
    "funds_shortage_cluster": "delay_retry",
    "unknown": "manual_flag",
}

VALID_ACTIONS = ["retry_later", "prompt_update", "delay_retry", "manual_flag"]

# ---- SIMULATED SUCCESS RATES ----
# How likely each action is to actually recover the payment, based on
# realistic assumptions (not guaranteed - retries don't always work):
#   - retry_later (after an outage ends): high success, the underlying
#     problem is temporary and usually resolves itself
#   - prompt_update (ask customer to update expired card): moderate -
#     depends on whether the customer actually responds
#   - delay_retry (wait for payday, then retry): moderate-high
#   - manual_flag (uncertain cause / high-stakes case): no automatic
#     recovery, needs a human - correctly reported as Rs.0 auto-recovered
ACTION_SUCCESS_RATE = {
    "retry_later": 0.80,
    "prompt_update": 0.40,
    "delay_retry": 0.65,
    "manual_flag": 0.0,
}

# Maximum retry attempts before giving up (stopping rule - avoid infinite loops)
MAX_RETRIES = 3


def resolve_action(row):
    """
    Use the LLM's recommended action if it's valid; otherwise fall back
    to the safe rule-based mapping from diagnosed cause. This keeps the
    pipeline robust even if the LLM output is ever missing/malformed.
    """
    action = row.get("recommended_action")
    if action in VALID_ACTIONS:
        return action
    return FALLBACK_CAUSE_TO_ACTION.get(row.get("diagnosed_cause", "unknown"), "manual_flag")


def simulate_recovery(action, amount, num_failed_txns):
    """
    Simulate attempting the recovery action on the affected transactions.
    Each individual transaction gets its own random success/fail roll.

    IMPORTANT: retries only make sense for actions where trying again
    could plausibly help (e.g. retry_later, after a temporary outage).
    For actions like prompt_update or delay_retry, the outcome depends
    on a real-world event (customer updates card, payday arrives) - not
    on repeated attempts - so we only roll ONCE per transaction for those.
    """
    success_rate = ACTION_SUCCESS_RATE[action]
    avg_amount_per_txn = amount / num_failed_txns if num_failed_txns > 0 else 0

    # Only 'retry_later' benefits from multiple attempts (transient issue)
    retries_allowed = MAX_RETRIES if action == "retry_later" else 1

    amount_recovered = 0
    num_recovered = 0

    for _ in range(num_failed_txns):
        if action == "manual_flag":
            continue

        recovered = False
        for attempt in range(retries_allowed):
            if random.random() < success_rate:
                recovered = True
                break

        if recovered:
            amount_recovered += avg_amount_per_txn
            num_recovered += 1

    num_still_failed = num_failed_txns - num_recovered
    return round(amount_recovered, 2), num_recovered, num_still_failed


def run_full_recovery_pipeline(diagnoses):
    """
    Execute the recommended action + simulate outcome for every
    diagnosed anomaly.
    """
    results = []

    for _, row in diagnoses.iterrows():
        action = resolve_action(row)
        recovered_amount, num_recovered, num_still_failed = simulate_recovery(
            action, row["recoverable_amount"], row["num_failed_in_window"]
        )

        results.append({
            "issuer": row["issuer"],
            "time_window": row["time_window"],
            "diagnosed_cause": row["diagnosed_cause"],
            "confidence": row["confidence"],
            "action_taken": action,
            "reasoning": row.get("reasoning", ""),
            "amount_at_risk": row["recoverable_amount"],
            "amount_recovered": recovered_amount,
            "num_txns_recovered": num_recovered,
            "num_txns_still_failed": num_still_failed,
        })

    return pd.DataFrame(results)


if __name__ == "__main__":
    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)
    diagnoses = diagnose_all_anomalies_llm(df, anomalies)

    results = run_full_recovery_pipeline(diagnoses)
    print(results.to_string(index=False))

    print(f"\n{'='*60}")
    print("OVERALL RECOVERY SUMMARY")
    print(f"{'='*60}")
    total_at_risk = results["amount_at_risk"].sum()
    total_recovered = results["amount_recovered"].sum()
    recovery_pct = (total_recovered / total_at_risk * 100) if total_at_risk > 0 else 0

    print(f"Total amount at risk:      Rs.{total_at_risk:,.2f}")
    print(f"Total amount recovered:    Rs.{total_recovered:,.2f}")
    print(f"Overall recovery rate:     {recovery_pct:.1f}%")

    print(f"\nBreakdown by action taken:")
    action_summary = results.groupby("action_taken").agg(
        num_events=("issuer", "count"),
        at_risk=("amount_at_risk", "sum"),
        recovered=("amount_recovered", "sum"),
    )
    print(action_summary.to_string())