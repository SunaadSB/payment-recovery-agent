"""
STEP 8: LLM-powered diagnosis + action recommendation layer
Replaces the rule-based lookup table with a real LLM call (Google Gemini,
free tier) that reasons about the failure pattern the way a human analyst
would, AND recommends the recovery action itself (not a fixed lookup) -
this is the actual "AI" part of the AI Revenue Recovery agent.

IMPORTANT: all anomalies are diagnosed in a SINGLE batched API call,
not one call per anomaly. The free tier only allows 20 requests/day,
so calling once per anomaly burns through that quota fast. Batching
keeps this to 1 call per full run.
"""

import os
import json
import time
import warnings
import pandas as pd
from dotenv import load_dotenv
from google import genai

warnings.filterwarnings("ignore", message="Direct use of automatic function calling")

from detect import load_transactions, add_time_window_column, \
    calculate_success_rates_by_issuer_window, flag_anomalies

load_dotenv()  # reads the .env file and loads GEMINI_API_KEY

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = "gemini-3.6-flash"  # primary model
GEMINI_MODEL_FALLBACK = "gemini-3.5-flash-lite"  # separate quota pool - used if
                                                    # the primary model's daily
                                                    # quota is exhausted

VALID_CAUSES = ["issuer_outage", "card_expiry_cluster", "funds_shortage_cluster", "unknown"]
VALID_ACTIONS = ["retry_later", "prompt_update", "delay_retry", "manual_flag"]

# Fallback mapping, used only if the LLM's action is missing/invalid -
# never let a bad LLM response silently break the pipeline
FALLBACK_CAUSE_TO_ACTION = {
    "issuer_outage": "retry_later",
    "card_expiry_cluster": "prompt_update",
    "funds_shortage_cluster": "delay_retry",
    "unknown": "manual_flag",
}


def get_failed_transactions_in_window(df, issuer, time_window):
    """Pull the actual failed transactions for one flagged window."""
    window_end = time_window + pd.Timedelta(hours=2)
    mask = (
        (df["issuer"] == issuer)
        & (df["timestamp"] >= time_window)
        & (df["timestamp"] < window_end)
        & (df["status"] == "failed")
    )
    return df[mask]


def build_batch_prompt(anomaly_summaries):
    """
    Build ONE prompt describing ALL flagged anomalies at once, so we only
    need a single API call for the whole batch instead of one call each.
    Asks the LLM to both diagnose the cause AND recommend the recovery
    action - not a fixed lookup table.
    """
    blocks = []
    for i, a in enumerate(anomaly_summaries):
        blocks.append(f"""
Anomaly #{i}:
  Issuer: {a['issuer']}
  Time window: {a['time_window']}
  Failed transactions: {a['total_failed']}
  Failure code breakdown: {json.dumps(a['failure_code_counts'])}
  Total amount affected: Rs.{a['total_amount']:,.2f}
""")

    prompt = f"""You are a payments risk analyst. Below are {len(anomaly_summaries)}
detected payment success-rate drops (anomalies), each identified by an index number.

{"".join(blocks)}

For EACH anomaly, do two things:

STEP A - Diagnose the most likely root cause. Choose exactly ONE of:
- "issuer_outage": failures concentrated in a short time window, dominated
  by timeout/technical failure codes - suggests the bank's system was down.
- "card_expiry_cluster": failures are card-expiry related.
- "funds_shortage_cluster": failures are insufficient-funds related.
- "unknown": the pattern is mixed/unclear and doesn't confidently fit one
  category - it is better to say unknown than to force a wrong guess.

STEP B - Recommend ONE recovery action that fits the diagnosed cause:
- "retry_later": appropriate for issuer_outage - the problem is usually
  temporary and retrying after it clears often works.
- "prompt_update": appropriate for card_expiry_cluster - ask the customer
  to update their card, since retrying won't fix an expired card.
- "delay_retry": appropriate for funds_shortage_cluster - wait for a
  likely payday/fund-availability window before retrying.
- "manual_flag": appropriate when cause is "unknown", when confidence is
  genuinely low, or when the amount at risk is very large AND the
  pattern is meaningfully ambiguous - route to a human instead of
  guessing on a genuinely uncertain, high-stakes case.

IMPORTANT - concrete confidence and action rules (follow these numeric
thresholds, do not default to caution beyond what they specify):
- If ONE failure code accounts for 70%+ of failures in the window, that
  is a CLEAR pattern - assign confidence 0.75-0.95 regardless of amount,
  and recommend the matching automated action (retry_later /
  prompt_update / delay_retry). A large amount alone is NOT a reason to
  lower confidence or switch to manual_flag when the pattern is this
  clear - clear evidence justifies automated action even on large sums.
- If ONE failure code accounts for 50-70% of failures (a clear
  plurality, some mixing), assign confidence 0.55-0.75. Recommend the
  matching automated action UNLESS the amount is VERY large (over
  Rs.30,000) - only then prefer manual_flag, since a moderately mixed
  pattern combined with very high stakes is the one case worth a
  human's extra scrutiny.
- If NO failure code exceeds 50% (genuinely mixed, no clear plurality),
  assign confidence below 0.5, diagnose "unknown", and recommend
  manual_flag regardless of amount - this pattern is genuinely
  ambiguous and guessing would not be justified at any stakes level.
- Do not treat "amount is somewhat large" alone as a reason for
  manual_flag when the failure-code pattern is clearly dominant (70%+).
  Reserve manual_flag for truly ambiguous patterns or the specific
  50-70%-dominant-plus-very-large-amount case above.

Briefly reflect which rule applied in your one-sentence reasoning.

Respond with ONLY a JSON array, one object per anomaly, in this exact
format and order, nothing else:
[
  {{"index": 0, "cause": "<category>", "confidence": <0.0-1.0>, "recommended_action": "<action>", "reasoning": "<one or two sentences, mention amount if it influenced your decision>"}},
  {{"index": 1, "cause": "<category>", "confidence": <0.0-1.0>, "recommended_action": "<action>", "reasoning": "<one or two sentences>"}}
]"""

    return prompt


def diagnose_all_anomalies_llm(df, anomalies):
    """
    Run LLM-based diagnosis + action recommendation on ALL flagged
    anomalies using a SINGLE batched API call. Falls back to
    'unknown' / 'manual_flag' for everything if the call fails entirely.
    """
    if len(anomalies) == 0:
        return pd.DataFrame()

    # Step 1: gather the underlying failed-transaction data for each anomaly
    anomaly_summaries = []
    failed_txns_by_index = {}

    for i, (_, anomaly) in enumerate(anomalies.iterrows()):
        failed_txns = get_failed_transactions_in_window(
            df, anomaly["issuer"], anomaly["time_window"]
        )
        failed_txns_by_index[i] = failed_txns

        anomaly_summaries.append({
            "issuer": anomaly["issuer"],
            "time_window": anomaly["time_window"],
            "total_failed": len(failed_txns),
            "failure_code_counts": failed_txns["failure_code"].value_counts().to_dict(),
            "total_amount": failed_txns["amount"].sum(),
        })

    # Step 2: make ONE API call for the whole batch
    prompt = build_batch_prompt(anomaly_summaries)
    llm_results = {}

    try:
        response = None
        last_error = None
        models_to_try = [GEMINI_MODEL, GEMINI_MODEL, GEMINI_MODEL_FALLBACK]  # 2 tries on primary, then fallback model
        for attempt, model_name in enumerate(models_to_try):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                if model_name != GEMINI_MODEL:
                    print(f"  [used fallback model {model_name} - primary model quota likely exhausted]")
                break
            except Exception as e:
                last_error = e
                is_last_attempt = attempt == len(models_to_try) - 1
                if not is_last_attempt:
                    wait_time = 5 * (attempt + 1)
                    print(f"  [retrying after error (attempt {attempt + 1}/{len(models_to_try)}): {e}]")
                    time.sleep(wait_time)

        if response is None:
            raise last_error

        response_text = response.text.strip()
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        parsed_list = json.loads(response_text)

        for item in parsed_list:
            idx = item.get("index")
            cause = item.get("cause", "unknown")
            if cause not in VALID_CAUSES:
                cause = "unknown"

            action = item.get("recommended_action", "manual_flag")
            if action not in VALID_ACTIONS:
                # Fall back to the safe rule-based mapping if the LLM
                # returned something we don't recognize
                action = FALLBACK_CAUSE_TO_ACTION.get(cause, "manual_flag")

            llm_results[idx] = {
                "cause": cause,
                "confidence": float(item.get("confidence", 0.0)),
                "action": action,
                "reasoning": item.get("reasoning", ""),
            }

    except Exception as e:
        print(f"  [Batch LLM call failed: {e}]")
        print("  Falling back to 'unknown'/'manual_flag' for all anomalies in this batch.")

    # Step 3: build the final results table
    diagnoses = []
    for i, anomaly_data in enumerate(anomaly_summaries):
        result = llm_results.get(i, {
            "cause": "unknown",
            "confidence": 0.0,
            "action": "manual_flag",
            "reasoning": "No LLM result available for this anomaly (batch call failed or index missing).",
        })

        print(f"  {anomaly_data['issuer']} {anomaly_data['time_window']} -> "
              f"{result['cause']} (confidence {result['confidence']}) "
              f"| action: {result['action']} | {result['reasoning']}")

        failed_txns = failed_txns_by_index[i]
        diagnoses.append({
            "issuer": anomaly_data["issuer"],
            "time_window": anomaly_data["time_window"],
            "diagnosed_cause": result["cause"],
            "confidence": result["confidence"],
            "recommended_action": result["action"],
            "reasoning": result["reasoning"],
            "num_failed_in_window": len(failed_txns),
            "recoverable_amount": failed_txns[failed_txns["failure_code"].notna()]["amount"].sum(),
        })

    return pd.DataFrame(diagnoses)


if __name__ == "__main__":
    df = load_transactions()
    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    reliable_groups, anomalies = flag_anomalies(grouped)

    print(f"Diagnosing {len(anomalies)} flagged anomalies using Gemini (1 batched call)...\n")

    diagnoses = diagnose_all_anomalies_llm(df, anomalies)

    print(f"\n{'='*70}")
    print(diagnoses[["issuer", "time_window", "diagnosed_cause", "confidence",
                      "recommended_action", "reasoning"]].to_string(index=False))