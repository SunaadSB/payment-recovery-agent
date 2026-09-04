"""
STEP 2: Detection layer
Scans transaction data and automatically flags anomalous drops in
success rate - without being told in advance where the problems are.
"""

import pandas as pd
from scipy import stats

# ---- CONFIG ----
TIME_WINDOW_HOURS = 2      # group transactions into 2-hour blocks
MIN_TXNS_IN_WINDOW = 5     # need at least this many transactions to judge a window
                            # (avoids flagging a window with only 1-2 transactions,
                            #  where one random failure looks like 100% failure rate)
NORMAL_SUCCESS_RATE = 0.95  # our known baseline (from Step 1)
DROP_THRESHOLD = 0.15       # flag if success rate falls 15+ percentage points below normal


def load_transactions():
    """Load the CSV we generated in Step 1, and parse timestamps properly."""
    df = pd.read_csv("data/transactions.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def add_time_window_column(df):
    """
    Round each transaction's timestamp DOWN to the nearest 2-hour block.
    e.g. 14:37 -> 14:00, 15:59 -> 14:00, 16:05 -> 16:00
    This lets us group transactions that happened "around the same time".
    """
    df["time_window"] = df["timestamp"].dt.floor(f"{TIME_WINDOW_HOURS}h")
    return df


def calculate_success_rates_by_issuer_window(df):
    """
    For every (issuer, time_window) combination, calculate:
    - how many transactions happened
    - how many succeeded
    - the success rate
    Only look at card/netbanking transactions, since those are the ones
    that have an issuer (UPI/wallet don't in our data).
    """
    # Only rows where issuer is not empty
    issuer_txns = df[df["issuer"].notna()]

    grouped = issuer_txns.groupby(["issuer", "time_window"]).agg(
        total_txns=("txn_id", "count"),
        successful_txns=("status", lambda x: (x == "success").sum()),
    ).reset_index()

    grouped["success_rate"] = grouped["successful_txns"] / grouped["total_txns"]

    return grouped


def flag_anomalies(grouped):
    """
    Flag groups as anomalies using a PROPER STATISTICAL TEST instead of a
    fixed threshold. This answers: "is this drop bigger than what pure
    random chance would produce, given how few transactions we saw?"

    We use a binomial test: if the true success rate is really 95%, how
    likely is it that we'd see THIS FEW successes out of THIS MANY tries,
    just by chance? If that's very unlikely (p-value < 0.01), we flag it.
    """
    reliable_groups = grouped[grouped["total_txns"] >= MIN_TXNS_IN_WINDOW].copy()

    p_values = []
    for _, row in reliable_groups.iterrows():
        result = stats.binomtest(
            k=int(row["successful_txns"]),
            n=int(row["total_txns"]),
            p=NORMAL_SUCCESS_RATE,
            alternative="less",  # we only care about DROPS, not spikes upward
        )
        p_values.append(result.pvalue)

    reliable_groups["p_value"] = p_values

    # p_value < 0.01 means: less than 1% chance this drop happened by
    # random luck alone -> genuinely suspicious, not noise
    SIGNIFICANCE_LEVEL = 0.01
    reliable_groups["is_anomaly"] = reliable_groups["p_value"] < SIGNIFICANCE_LEVEL

    anomalies = reliable_groups[reliable_groups["is_anomaly"]].sort_values("p_value")

    return reliable_groups, anomalies


if __name__ == "__main__":
    df = load_transactions()
    print(f"Loaded {len(df)} transactions")
    print(f"Overall success rate: {(df['status'] == 'success').mean():.2%}\n")

    df = add_time_window_column(df)
    grouped = calculate_success_rates_by_issuer_window(df)
    print(f"Created {len(grouped)} (issuer, time_window) groups total")

    reliable_groups, anomalies = flag_anomalies(grouped)
    print(f"{len(reliable_groups)} groups had enough volume (>= {MIN_TXNS_IN_WINDOW} txns) to judge reliably")

    print(f"\n{'='*60}")
    print(f"FLAGGED ANOMALIES: {len(anomalies)}")
    print(f"{'='*60}")
    print(anomalies.to_string(index=False))