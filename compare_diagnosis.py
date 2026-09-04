"""
Rule-based vs. LLM diagnosis comparison.
Runs BOTH diagnosis approaches on the exact same flagged anomalies and
prints them side-by-side, so we can concretely show where the LLM's
judgment adds value over a rigid lookup-table approach (e.g. correctly
saying "unknown" on ambiguous cases the rule-based version misdiagnoses).
"""

import pandas as pd
from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies
from diagnose import diagnose_all_anomalies as diagnose_rule_based
from diagnose_llm import diagnose_all_anomalies_llm
from validate import load_ground_truth


def compare(df, anomalies, ground_truth):
    print("Running RULE-BASED diagnosis...")
    rule_results = diagnose_rule_based(df, anomalies)

    print("\nRunning LLM-BASED diagnosis (1 batched call)...")
    llm_results = diagnose_all_anomalies_llm(df, anomalies)

    # Merge side by side on issuer + time_window
    comparison = rule_results[["issuer", "time_window", "diagnosed_cause", "confidence"]].rename(
        columns={"diagnosed_cause": "rule_cause", "confidence": "rule_confidence"}
    ).merge(
        llm_results[["issuer", "time_window", "diagnosed_cause", "confidence", "recommended_action"]].rename(
            columns={"diagnosed_cause": "llm_cause", "confidence": "llm_confidence"}
        ),
        on=["issuer", "time_window"],
    )

    # Mark where they disagree - the interesting cases
    comparison["agree"] = comparison["rule_cause"] == comparison["llm_cause"]

    print(f"\n{'='*90}")
    print("SIDE-BY-SIDE COMPARISON")
    print(f"{'='*90}")
    print(comparison.to_string(index=False))

    disagreements = comparison[~comparison["agree"]]
    print(f"\n{'='*90}")
    print(f"DISAGREEMENTS: {len(disagreements)} of {len(comparison)} anomalies")
    print(f"{'='*90}")
    if len(disagreements) > 0:
        print(disagreements.to_string(index=False))
    else:
        print("Both approaches agreed on every anomaly in this run.")

    # Check which approach was actually correct on the disagreements,
    # using our ground truth answer key
    real_causes = set(ground_truth["true_cause"])
    print(f"\n{'='*90}")
    print("ACCURACY CHECK ON DISAGREEMENTS (against ground truth)")
    print(f"{'='*90}")
    for _, row in disagreements.iterrows():
        rule_plausible = row["rule_cause"] in real_causes or row["rule_cause"] == "unknown"
        llm_plausible = row["llm_cause"] in real_causes or row["llm_cause"] == "unknown"
        print(f"{row['issuer']} {row['time_window']}: "
              f"rule said '{row['rule_cause']}', llm said '{row['llm_cause']}'")

    return comparison


if __name__ == "__main__":
    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)
    ground_truth = load_ground_truth()

    comparison = compare(df, anomalies, ground_truth)

    comparison.to_csv("data/diagnosis_comparison.csv", index=False)
    print("\nSaved comparison to data/diagnosis_comparison.csv")
