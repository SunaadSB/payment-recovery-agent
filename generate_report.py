"""
Generates a polished, self-contained HTML dashboard from the pipeline's
output files (data/pipeline_results.csv, data/incident_summary.txt).
No server needed - just open the output file in any browser.

Run this AFTER run_pipeline.py, since it reads files that script saves.
"""

import pandas as pd
import json
from datetime import datetime

ACTION_COLORS = {
    "retry_later": "#16a34a",       # green - auto action
    "delay_retry": "#16a34a",       # green - auto action
    "prompt_update": "#2563eb",     # blue - customer-facing action
    "manual_flag": "#d97706",       # amber - human review
}

ACTION_LABELS = {
    "retry_later": "Auto-Retry",
    "delay_retry": "Delayed Retry",
    "prompt_update": "Customer Prompt",
    "manual_flag": "Manual Review",
}

CAUSE_LABELS = {
    "issuer_outage": "Issuer Outage",
    "card_expiry_cluster": "Card Expiry Cluster",
    "funds_shortage_cluster": "Funds Shortage Cluster",
    "unknown": "Unknown / Ambiguous",
}


def load_validation_metrics():
    try:
        with open("data/validation_metrics.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def load_incident_summary():
    try:
        with open("data/incident_summary.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def build_event_card(row):
    action = row["action_taken"]
    color = ACTION_COLORS.get(action, "#6b7280")
    action_label = ACTION_LABELS.get(action, action)
    cause_label = CAUSE_LABELS.get(row["diagnosed_cause"], row["diagnosed_cause"])

    recovered = row["amount_recovered"]
    at_risk = row["amount_at_risk"]
    pct = (recovered / at_risk * 100) if at_risk > 0 else 0

    total_txns = row["num_txns_recovered"] + row["num_txns_still_failed"]

    return f"""
    <div class="event-card">
      <div class="event-header">
        <div>
          <span class="issuer-tag">{row['issuer']}</span>
          <span class="time-tag">{row['time_window']}</span>
        </div>
        <span class="action-badge" style="background:{color}">{action_label}</span>
      </div>
      <div class="event-body">
        <div class="cause-line">
          <strong>{cause_label}</strong>
          <span class="confidence">confidence {row['confidence']:.2f}</span>
        </div>
        <p class="reasoning">{row['reasoning']}</p>
        <div class="amount-bar-wrap">
          <div class="amount-bar-bg">
            <div class="amount-bar-fill" style="width:{pct:.1f}%; background:{color}"></div>
          </div>
          <div class="amount-labels">
            <span>Rs.{recovered:,.2f} recovered</span>
            <span>of Rs.{at_risk:,.2f} at risk</span>
          </div>
        </div>
        <div class="txn-count">{row['num_txns_recovered']} / {total_txns} transactions recovered</div>
      </div>
    </div>
    """


def generate_html_report():
    results = pd.read_csv("data/pipeline_results.csv")
    summary_text = load_incident_summary()
    validation = load_validation_metrics()

    total_at_risk = results["amount_at_risk"].sum()
    total_recovered = results["amount_recovered"].sum()
    recovery_pct = (total_recovered / total_at_risk * 100) if total_at_risk > 0 else 0
    num_auto = (results["amount_recovered"] > 0).sum()
    num_manual = (results["action_taken"] == "manual_flag").sum()

    event_cards_html = "\n".join(
        build_event_card(row) for _, row in results.sort_values("amount_at_risk", ascending=False).iterrows()
    )

    summary_block = ""
    if summary_text:
        summary_block = f"""
        <div class="summary-box">
          <div class="summary-label">AI-Generated Incident Summary</div>
          <p class="summary-text">{summary_text}</p>
        </div>
        """

    validation_block = ""
    if validation:
        def score_class(pct):
            if pct >= 80:
                return "green"
            if pct >= 50:
                return "amber"
            return "red"

        validation_block = f"""
        <div class="section-title">Validation Against Ground Truth</div>
        <div class="validation-grid">
          <div class="validation-card">
            <div class="validation-value {score_class(validation['recall_pct'])}">{validation['recall_pct']:.1f}%</div>
            <div class="validation-label">Recall</div>
            <div class="validation-detail">{validation['recall_caught']} / {validation['recall_total']} real events caught</div>
          </div>
          <div class="validation-card">
            <div class="validation-value {score_class(validation['diagnosis_accuracy_pct'])}">{validation['diagnosis_accuracy_pct']:.1f}%</div>
            <div class="validation-label">Diagnosis Accuracy</div>
            <div class="validation-detail">{validation['diagnosis_correct']} / {validation['diagnosis_total']} correctly diagnosed</div>
          </div>
          <div class="validation-card">
            <div class="validation-value {score_class(validation['precision_pct'])}">{validation['precision_pct']:.1f}%</div>
            <div class="validation-label">Precision</div>
            <div class="validation-detail">{validation['precision_true_positives']} / {validation['precision_total_flagged']} flags were real</div>
          </div>
        </div>
        <p class="validation-note">
          Measured against a synthetic dataset with known ground truth
          (deliberately injected failure patterns, including one
          intentionally ambiguous case to test honest uncertainty).
        </p>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Payment Recovery Agent - Run Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f8fafc;
    color: #1e293b;
    padding: 40px 20px;
  }}
  .container {{ max-width: 900px; margin: 0 auto; }}
  header {{ margin-bottom: 32px; }}
  h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; font-size: 14px; }}

  .metrics-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }}
  .metric-card {{
    background: white;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    border: 1px solid #e2e8f0;
  }}
  .metric-value {{ font-size: 24px; font-weight: 700; }}
  .metric-label {{ font-size: 12px; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .metric-value.green {{ color: #16a34a; }}
  .metric-value.amber {{ color: #d97706; }}

  .validation-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 8px;
  }}
  .validation-card {{
    background: white;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    border: 1px solid #e2e8f0;
    text-align: center;
  }}
  .validation-value {{ font-size: 28px; font-weight: 800; }}
  .validation-value.green {{ color: #16a34a; }}
  .validation-value.amber {{ color: #d97706; }}
  .validation-value.red {{ color: #dc2626; }}
  .validation-label {{ font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #334155; margin-top: 6px; }}
  .validation-detail {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
  .validation-note {{ font-size: 12px; color: #94a3b8; margin-bottom: 28px; font-style: italic; }}

  .summary-box {{
    background: linear-gradient(135deg, #eff6ff, #f0f9ff);
    border: 1px solid #bfdbfe;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 28px;
  }}
  .summary-label {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #2563eb;
    margin-bottom: 8px;
  }}
  .summary-text {{ font-size: 15px; line-height: 1.6; color: #1e3a5f; }}

  .section-title {{
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 14px;
    color: #334155;
  }}

  .event-card {{
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    margin-bottom: 14px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }}
  .event-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 20px;
    background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }}
  .issuer-tag {{ font-weight: 700; font-size: 15px; margin-right: 10px; }}
  .time-tag {{ font-size: 13px; color: #64748b; }}
  .action-badge {{
    color: white;
    font-size: 11px;
    font-weight: 700;
    padding: 5px 12px;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .event-body {{ padding: 16px 20px; }}
  .cause-line {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .cause-line strong {{ font-size: 15px; }}
  .confidence {{ font-size: 12px; color: #94a3b8; }}
  .reasoning {{ font-size: 13px; color: #475569; line-height: 1.5; margin-bottom: 14px; }}
  .amount-bar-bg {{
    width: 100%;
    height: 8px;
    background: #e2e8f0;
    border-radius: 999px;
    overflow: hidden;
  }}
  .amount-bar-fill {{ height: 100%; border-radius: 999px; }}
  .amount-labels {{
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: #64748b;
    margin-top: 6px;
  }}
  .txn-count {{ font-size: 11px; color: #94a3b8; margin-top: 8px; }}

  footer {{
    margin-top: 32px;
    text-align: center;
    font-size: 12px;
    color: #94a3b8;
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Payment Degradation Recovery Agent</h1>
    <div class="subtitle">Run report generated {datetime.now().strftime('%d %b %Y, %H:%M')}</div>
  </header>

  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-value">{len(results)}</div>
      <div class="metric-label">Anomalies Detected</div>
    </div>
    <div class="metric-card">
      <div class="metric-value">Rs.{total_at_risk:,.0f}</div>
      <div class="metric-label">Amount At Risk</div>
    </div>
    <div class="metric-card">
      <div class="metric-value green">Rs.{total_recovered:,.0f}</div>
      <div class="metric-label">Amount Recovered</div>
    </div>
    <div class="metric-card">
      <div class="metric-value {'green' if recovery_pct >= 50 else 'amber'}">{recovery_pct:.1f}%</div>
      <div class="metric-label">Recovery Rate</div>
    </div>
  </div>

  {summary_block}

  {validation_block}

  <div class="section-title">Events (sorted by amount at risk)</div>
  {event_cards_html}

  <footer>
    {num_auto} auto-recovered &middot; {num_manual} routed to manual review &middot;
    Full audit trail in data/audit_trail.json
  </footer>
</div>
</body>
</html>"""

    with open("data/report.html", "w", encoding="utf-8") as f:
        f.write(html)

    return "data/report.html"


if __name__ == "__main__":
    path = generate_html_report()
    print(f"Report generated: {path}")
    print("Open this file in your browser to view it.")