"""
STEP 1: Synthetic transaction data generator
Generates realistic payment transactions with injected failure patterns.
"""

import pandas as pd
import numpy as np
from faker import Faker
from datetime import datetime, timedelta
import random

# Make results reproducible - same "random" data every time we run this
random.seed(42)
np.random.seed(42)
fake = Faker()

# ---- CONFIG ----
NUM_DAYS = 8                  # simulate 8 days of transactions
TXNS_PER_DAY = 500            # ~500 transactions per day -> ~4000 total
                               # (increased from 120 so each 2-hour/issuer
                               #  bucket has enough volume to detect reliably)
START_DATE = datetime(2026, 8, 17)

ISSUERS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak"]
PAYMENT_METHODS = ["card", "upi", "netbanking", "wallet"]

BASELINE_FAIL_RATE = 0.05     # 5% of transactions normally fail, no special reason


def random_timestamp_on_day(day):
    """Pick a random time during business hours (9am - 11pm) on a given day."""
    hour = random.randint(9, 23)
    minute = random.randint(0, 59)
    return day.replace(hour=hour, minute=minute, second=0)


def generate_baseline_transactions():
    """
    Generate the 'normal' transaction stream - most succeed,
    a small % fail randomly for no special reason (baseline noise).
    """
    rows = []
    txn_counter = 1

    for day_num in range(NUM_DAYS):
        current_day = START_DATE + timedelta(days=day_num)

        for _ in range(TXNS_PER_DAY):
            txn_id = f"TXN{txn_counter:05d}"
            customer_id = f"CUST{random.randint(1, 300):04d}"
            timestamp = random_timestamp_on_day(current_day)
            amount = round(random.uniform(150, 8000), 2)
            method = random.choice(PAYMENT_METHODS)
            issuer = random.choice(ISSUERS) if method in ["card", "netbanking"] else None

            # Baseline: 95% succeed, 5% fail randomly (normal noise)
            is_success = random.random() > BASELINE_FAIL_RATE

            row = {
                "txn_id": txn_id,
                "timestamp": timestamp,
                "customer_id": customer_id,
                "amount": amount,
                "payment_method": method,
                "issuer": issuer,
                "status": "success" if is_success else "failed",
                "failure_code": None if is_success else "generic_decline",
            }
            rows.append(row)
            txn_counter += 1

    return pd.DataFrame(rows), txn_counter


def inject_issuer_outage(df, ground_truth_events):
    """
    PATTERN 1: Issuer outage.
    Pick one bank + a 2-4 hour window on one day.
    During that window, most card/netbanking transactions through that
    bank fail with 'issuer_timeout' - simulating the bank's system being down.
    """
    # Pick which bank has the outage, and which day
    outage_issuer = random.choice(ISSUERS)
    outage_day = START_DATE + timedelta(days=random.randint(2, NUM_DAYS - 2))
    outage_start_hour = random.randint(10, 18)
    outage_duration_hours = random.randint(2, 4)
    outage_end_hour = outage_start_hour + outage_duration_hours

    outage_start = outage_day.replace(hour=outage_start_hour, minute=0)
    outage_end = outage_day.replace(hour=min(outage_end_hour, 23), minute=59)

    # Find every transaction that falls inside this window AND uses this issuer
    mask = (
        (df["issuer"] == outage_issuer)
        & (df["timestamp"] >= outage_start)
        & (df["timestamp"] <= outage_end)
    )
    affected_indices = df[mask].index

    recoverable_amount = 0
    for idx in affected_indices:
        # During an outage, ~75% of attempts fail (not 100% - some might
        # succeed via a different route, which is realistic)
        if random.random() < 0.75:
            df.loc[idx, "status"] = "failed"
            df.loc[idx, "failure_code"] = "issuer_timeout"
            recoverable_amount += df.loc[idx, "amount"]

    # Save this event to our ground truth answer key
    ground_truth_events.append({
        "event_id": f"EVT_OUTAGE_{outage_issuer}",
        "window_start": outage_start,
        "window_end": outage_end,
        "affected_issuer_or_method": outage_issuer,
        "true_cause": "issuer_outage",
        "expected_action": "retry_later",
        "recoverable_amount": round(recoverable_amount, 2),
    })

    return df, ground_truth_events


def inject_card_expiry_cluster(df, ground_truth_events):
    """
    PATTERN 2: Card expiry cluster.
    Unlike an outage, this is scattered - random customers, random times,
    no single bank or time window. Tests whether the detective can tell
    "many small unrelated problems" apart from "one big systemic problem".
    """
    # Only card transactions can have an expired card
    card_txns = df[df["payment_method"] == "card"]

    # Pick ~60 random card transactions across the whole dataset to mark as expired
    num_expired = 60
    affected_indices = random.sample(list(card_txns.index), num_expired)

    recoverable_amount = 0
    for idx in affected_indices:
        df.loc[idx, "status"] = "failed"
        df.loc[idx, "failure_code"] = "card_expired"
        recoverable_amount += df.loc[idx, "amount"]

    ground_truth_events.append({
        "event_id": "EVT_CARD_EXPIRY_CLUSTER",
        "window_start": None,          # no specific time window - scattered
        "window_end": None,
        "affected_issuer_or_method": "card",
        "true_cause": "card_expiry_cluster",
        "expected_action": "prompt_update",
        "recoverable_amount": round(recoverable_amount, 2),
    })

    return df, ground_truth_events


def inject_insufficient_funds_cluster(df, ground_truth_events):
    """
    PATTERN 3: Insufficient funds cluster.
    Bunched near month-end (a common real-world pattern - people run low
    on money before their next payday/salary credit).
    """
    # Find transactions in the last 2 days of our simulated period (month-end)
    month_end_start = START_DATE + timedelta(days=NUM_DAYS - 2)
    month_end_txns = df[df["timestamp"] >= month_end_start]

    # Mark ~80 of them as insufficient funds failures
    num_affected = min(80, len(month_end_txns))
    affected_indices = random.sample(list(month_end_txns.index), num_affected)

    recoverable_amount = 0
    for idx in affected_indices:
        df.loc[idx, "status"] = "failed"
        df.loc[idx, "failure_code"] = "insufficient_funds"
        recoverable_amount += df.loc[idx, "amount"]

    ground_truth_events.append({
        "event_id": "EVT_FUNDS_SHORTAGE_CLUSTER",
        "window_start": month_end_start,
        "window_end": df["timestamp"].max(),
        "affected_issuer_or_method": "all",
        "true_cause": "funds_shortage_cluster",
        "expected_action": "delay_retry",
        "recoverable_amount": round(recoverable_amount, 2),
    })

    return df, ground_truth_events


def inject_ambiguous_cluster(df, ground_truth_events):
    """
    PATTERN 4: Genuinely ambiguous cluster - a real stress test.
    Unlike the other 3 patterns (each dominated by one clear failure_code),
    this one deliberately mixes issuer_timeout, card_expired, and
    insufficient_funds roughly evenly, concentrated in one issuer + time
    window. No single cause dominates (each code stays under 45% of
    failures) - a genuinely careful diagnosis SHOULD say "unknown" here,
    not force a confident guess. This tests whether the agent is honest
    about uncertainty rather than just pattern-matching a majority code.
    """
    ambiguous_issuer = random.choice(ISSUERS)
    ambiguous_day = START_DATE + timedelta(days=random.randint(1, NUM_DAYS - 1))
    window_start_hour = random.randint(10, 18)

    window_start = ambiguous_day.replace(hour=window_start_hour, minute=0)
    window_end = ambiguous_day.replace(hour=min(window_start_hour + 2, 23), minute=59)

    mask = (
        (df["issuer"] == ambiguous_issuer)
        & (df["timestamp"] >= window_start)
        & (df["timestamp"] <= window_end)
        & (df["status"] == "success")  # only convert currently-successful txns
    )
    candidate_indices = list(df[mask].index)

    # Take a decent-sized cluster and split failures roughly evenly across
    # 3 different codes, so no single code dominates (deliberately mixed)
    num_to_fail = min(9, len(candidate_indices))
    affected_indices = random.sample(candidate_indices, num_to_fail)

    mixed_codes = ["issuer_timeout", "card_expired", "insufficient_funds"]
    recoverable_amount = 0
    for i, idx in enumerate(affected_indices):
        code = mixed_codes[i % len(mixed_codes)]  # evenly rotate through codes
        df.loc[idx, "status"] = "failed"
        df.loc[idx, "failure_code"] = code
        recoverable_amount += df.loc[idx, "amount"]

    ground_truth_events.append({
        "event_id": f"EVT_AMBIGUOUS_{ambiguous_issuer}",
        "window_start": window_start,
        "window_end": window_end,
        "affected_issuer_or_method": ambiguous_issuer,
        "true_cause": "unknown",  # the CORRECT diagnosis is genuine uncertainty
        "expected_action": "manual_flag",
        "recoverable_amount": round(recoverable_amount, 2),
    })

    return df, ground_truth_events


if __name__ == "__main__":
    df, next_txn_num = generate_baseline_transactions()
    print(f"Generated {len(df)} baseline transactions")
    print(f"Baseline success rate: {(df['status'] == 'success').mean():.2%}\n")

    # Ground truth events list - our "answer key" for later validation
    ground_truth_events = []

    df, ground_truth_events = inject_issuer_outage(df, ground_truth_events)
    df, ground_truth_events = inject_card_expiry_cluster(df, ground_truth_events)
    df, ground_truth_events = inject_insufficient_funds_cluster(df, ground_truth_events)
    df, ground_truth_events = inject_ambiguous_cluster(df, ground_truth_events)

    print("All injected events (our answer key):")
    for event in ground_truth_events:
        print(f"  - {event['event_id']}: {event['true_cause']} -> recoverable ₹{event['recoverable_amount']}")

    print(f"\nFinal success rate after all injections: {(df['status'] == 'success').mean():.2%}")
    print(f"Total failed transactions: {(df['status'] == 'failed').sum()} out of {len(df)}")

    # Save the data so we can use it in later steps
    df.to_csv("data/transactions.csv", index=False)
    print("\nSaved to data/transactions.csv")

    # Also save the ground truth events so validate.py can check against them
    ground_truth_df = pd.DataFrame(ground_truth_events)
    ground_truth_df.to_csv("data/ground_truth_events.csv", index=False)
    print("Saved to data/ground_truth_events.csv")