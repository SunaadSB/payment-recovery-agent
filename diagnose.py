"""
STEP 3: Diagnosis layer
For each flagged anomaly, looks at the failure_codes of the affected
transactions and classifies the likely root cause.
"""

import pandas as pd
from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies


def get_failed_transactions_in_window(df, issuer, time_window):
    """
    Given one flagged (issuer, time_window) anomaly, pull out the actual
    failed transaction rows that fall inside it - so we can inspect
    WHY they failed (their failure_code).
    """
    window_end = time_window + pd.Timedelta(hours=2)  # matches TIME_WINDOW_HOURS

    mask = (
        (df["issuer"] == issuer)
        & (df["timestamp"] >= time_window)
        & (df["timestamp"] < window_end)
        & (df["status"] == "failed")
    )
    return df[mask]


def diagnose_cause(failed_txns):
    """
    Look at the mix of failure_codes in this window and decide the
    most likely root cause, using simple rules:

    - Mostly 'issuer_timeout' -> issuer_outage
    - Mostly 'card_expired' -> card_expiry_cluster
    - Mostly 'insufficient_funds' -> funds_shortage_cluster
    - Mixed / unclear -> unknown (flag for manual review)
    """
    if len(failed_txns) == 0:
        return "unknown", 0.0

    # Count how often each failure_code appears in this window
    code_counts = failed_txns["failure_code"].value_counts()
    top_code = code_counts.index[0]
    top_code_share = code_counts.iloc[0] / len(failed_txns)

    # Map failure codes to causes
    code_to_cause = {
        "issuer_timeout": "issuer_outage",
        "card_expired": "card_expiry_cluster",
        "insufficient_funds": "funds_shortage_cluster",
        "generic_decline": "unknown",
    }

    cause = code_to_cause.get(top_code, "unknown")

    # Only trust the diagnosis if one failure_code clearly dominates (>60%)
    # Otherwise it's a mixed bag - safer to say "unknown" than guess wrong
    CONFIDENCE_THRESHOLD = 0.60
    if top_code_share < CONFIDENCE_THRESHOLD:
        cause = "unknown"

    return cause, round(top_code_share, 2)


def diagnose_all_anomalies(df, anomalies):
    """
    Run diagnosis on every flagged anomaly, and attach the result.
    """
    diagnoses = []

    for _, anomaly in anomalies.iterrows():
        failed_txns = get_failed_transactions_in_window(
            df, anomaly["issuer"], anomaly["time_window"]
        )
        cause, confidence = diagnose_cause(failed_txns)

        diagnoses.append({
            "issuer": anomaly["issuer"],
            "time_window": anomaly["time_window"],
            "total_txns": anomaly["total_txns"],
            "success_rate": anomaly["success_rate"],
            "p_value": anomaly["p_value"],
            "diagnosed_cause": cause,
            "confidence": confidence,
            "num_failed_in_window": len(failed_txns),
            "recoverable_amount": failed_txns[failed_txns["failure_code"].notna()]["amount"].sum(),
        })

    return pd.DataFrame(diagnoses)


if __name__ == "__main__":
    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)

    print(f"Diagnosing {len(anomalies)} flagged anomalies...\n")

    diagnoses = diagnose_all_anomalies(df, anomalies)
    print(diagnoses.to_string(index=False))

    print(f"\n{'='*60}")
    print("SUMMARY BY DIAGNOSED CAUSE")
    print(f"{'='*60}")
    summary = diagnoses.groupby("diagnosed_cause").agg(
        num_events=("issuer", "count"),
        total_recoverable=("recoverable_amount", "sum"),
    )
    print(summary.to_string())
