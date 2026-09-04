"""
STEP 5: Validation against ground truth
Compares what our pipeline detected/diagnosed against the real answer
key we saved when generating the data - giving us honest accuracy numbers.
"""

import pandas as pd
from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies
from diagnose import diagnose_all_anomalies
from recover import run_full_recovery_pipeline


def load_ground_truth():
    """Load the answer key saved by generate_data.py"""
    gt = pd.read_csv("data/ground_truth_events.csv")
    gt["window_start"] = pd.to_datetime(gt["window_start"])
    gt["window_end"] = pd.to_datetime(gt["window_end"])
    return gt


def check_event_was_detected(gt_event, results):
    """
    For one real ground-truth event (e.g. the ICICI outage), check if
    ANY of our flagged results overlap with it in TIME AND ISSUER.
    An outage spanning several 2-hour windows can show up as multiple
    flagged rows - that's fine, we just need at least one match.
    """
    if pd.isna(gt_event["window_start"]):
        # Card expiry cluster has no specific time window (it's scattered) -
        # so we check by matching diagnosed cause type instead
        matches = results[results["diagnosed_cause"] == gt_event["true_cause"]]
        return len(matches) > 0, matches

    # For time-bound events (outage, funds shortage, ambiguous cluster),
    # require time overlap. Also require issuer match UNLESS this event
    # is marked "all" (e.g. the funds-shortage cluster, which is
    # deliberately scattered across every issuer, not one specific bank).
    if gt_event["affected_issuer_or_method"] == "all":
        matches = results[
            (results["time_window"] >= gt_event["window_start"] - pd.Timedelta(hours=2))
            & (results["time_window"] <= gt_event["window_end"])
        ]
    else:
        matches = results[
            (results["issuer"] == gt_event["affected_issuer_or_method"])
            & (results["time_window"] >= gt_event["window_start"] - pd.Timedelta(hours=2))
            & (results["time_window"] <= gt_event["window_end"])
        ]
    return len(matches) > 0, matches


def result_matches_any_ground_truth_event(result_row, ground_truth):
    """
    For ONE flagged result, check if it genuinely overlaps ANY real
    ground-truth event (by issuer + time, or by cause for windowless
    events). This is used for precision - it must be based on actual
    overlap with a real event, NOT just "was the diagnosed label one
    that happens to also be a real cause somewhere" (that check breaks
    now that 'unknown' is itself a legitimate true_cause for the
    ambiguous-cluster event, and would let noise incorrectly count).
    """
    for _, gt_event in ground_truth.iterrows():
        if pd.isna(gt_event["window_start"]):
            if result_row["diagnosed_cause"] == gt_event["true_cause"]:
                return True
        else:
            same_issuer = (
                gt_event["affected_issuer_or_method"] == "all"
                or result_row["issuer"] == gt_event["affected_issuer_or_method"]
            )
            time_overlap = (
                result_row["time_window"] >= gt_event["window_start"] - pd.Timedelta(hours=2)
                and result_row["time_window"] <= gt_event["window_end"]
            )
            if same_issuer and time_overlap:
                return True
    return False


def validate_pipeline(ground_truth, results):
    """
    Produce a clear report:
    - RECALL: of the real events we injected, how many did we catch?
    - DIAGNOSIS ACCURACY: of the ones we caught, did we get the cause right?
      (For the ambiguous-cluster event, "correct" specifically means the
      agent honestly said "unknown" rather than forcing a confident guess.)
    - PRECISION: of everything we flagged, how many overlapped a real
      event vs. were pure statistical noise?
    """
    print(f"{'='*70}")
    print("RECALL CHECK: Did we catch every real injected event?")
    print(f"{'='*70}")

    caught_count = 0
    correct_diagnosis_count = 0

    for _, gt_event in ground_truth.iterrows():
        was_detected, matches = check_event_was_detected(gt_event, results)
        status = "CAUGHT" if was_detected else "MISSED"
        note = " (correct answer here is 'unknown' - testing honesty under ambiguity)" \
            if gt_event["true_cause"] == "unknown" else ""
        print(f"\n{gt_event['event_id']} ({gt_event['true_cause']}){note}: {status}")

        if was_detected:
            caught_count += 1
            diagnosed_causes = matches["diagnosed_cause"].unique()
            correct = gt_event["true_cause"] in diagnosed_causes
            if correct:
                correct_diagnosis_count += 1
            print(f"  Diagnosed as: {list(diagnosed_causes)} "
                  f"{'(CORRECT)' if correct else '(WRONG)'}")

    print(f"\n{'='*70}")
    print("PRECISION CHECK: Of everything we flagged, how much was real?")
    print(f"{'='*70}")

    results["is_true_positive"] = results.apply(
        lambda row: result_matches_any_ground_truth_event(row, ground_truth), axis=1
    )
    true_positives = results["is_true_positive"].sum()
    false_positives = (~results["is_true_positive"]).sum()

    print(f"Total flagged: {len(results)}")
    print(f"  True positives (overlaps a real injected event):  {true_positives}")
    print(f"  False positives (pure statistical noise):          {false_positives}")

    print(f"\n{'='*70}")
    print("FINAL SCORECARD")
    print(f"{'='*70}")
    recall = caught_count / len(ground_truth) * 100
    diagnosis_accuracy = (correct_diagnosis_count / caught_count * 100) if caught_count > 0 else 0
    precision = (true_positives / len(results) * 100) if len(results) > 0 else 0

    print(f"Recall (real events caught):        {caught_count}/{len(ground_truth)} = {recall:.1f}%")
    print(f"Diagnosis accuracy (of those caught): {correct_diagnosis_count}/{caught_count} = {diagnosis_accuracy:.1f}%")
    print(f"Precision (flags that were real):    {true_positives}/{len(results)} = {precision:.1f}%")

    return {
        "recall_caught": int(caught_count),
        "recall_total": int(len(ground_truth)),
        "recall_pct": round(recall, 1),
        "diagnosis_correct": int(correct_diagnosis_count),
        "diagnosis_total": int(caught_count),
        "diagnosis_accuracy_pct": round(diagnosis_accuracy, 1),
        "precision_true_positives": int(true_positives),
        "precision_total_flagged": int(len(results)),
        "precision_pct": round(precision, 1),
    }


if __name__ == "__main__":
    ground_truth = load_ground_truth()

    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)
    diagnoses = diagnose_all_anomalies(df, anomalies)
    results = run_full_recovery_pipeline(diagnoses)

    validate_pipeline(ground_truth, results)