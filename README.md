# Payment Degradation → Root Cause → Recovery Agent

**Track:** AI Revenue Recovery — Razorpay Buildathon

An agent that detects abnormal drops in payment success rates, diagnoses
the likely root cause, and executes a bounded recovery action — reporting
real, measured amounts recovered against a known ground truth.

---

## The Problem

Payment success rates degrade silently and for different reasons: an
issuing bank has a temporary outage, a batch of customer cards expire
around the same billing cycle, or customers run low on funds near
month-end. Each looks identical on the surface (a "failed" status), but
requires a completely different fix. Today this is usually caught and
diagnosed manually — while revenue leaks in the meantime.

This project closes that loop automatically: **detect → diagnose → act →
measure**.

### How this relates to Razorpay's existing tools

Razorpay already has strong tools in this space - **Optimizer** (AI-powered
gateway routing using 2M+ data points to boost success rates in
real-time) and **In-Session Retries** (letting a failed card payment
retry within the same checkout session). Both act *in the moment*, on
a single transaction, optimizing for immediate conversion.

This project is a complementary, one-layer-back capability: it looks
at *patterns* of failures over time, diagnoses the systemic root cause
with an auditable explanation (not just a routing decision), and makes
a risk-aware call on whether to auto-act or escalate to a human -
explicitly designed to say "I don't know" rather than force a guess.
It's less "retry this transaction now" and more "why is this happening,
and should we trust an automated fix" - a triage and root-cause layer
that could sit alongside Optimizer's real-time routing.

---

## Architecture

```
┌────────────────────────┐
│  generate_data.py      │   Creates synthetic transaction stream (4,000 txns,
│  (Step 1)              │   8 days) with 3 deliberately injected failure
│                        │   patterns + a saved ground-truth answer key.
└──────────┬─────────────┘
           │ data/transactions.csv
           │ data/ground_truth_events.csv
           ▼
┌────────────────────────┐
│  detect.py             │   Groups transactions by (issuer, 2-hour window),
│  (Step 2)              │   calculates success rate per group, and flags
│                        │   statistically significant drops using a
│                        │   binomial test (not a naive fixed threshold).
└──────────┬─────────────┘
           │ flagged anomalies
           ▼
┌────────────────────────┐
│  diagnose_llm.py       │   For each flagged anomaly, sends the failure
│  (Step 3, AI step)     │   pattern to Gemini (Google's LLM, free tier) in
│                        │   a SINGLE batched call. The LLM both diagnoses
│                        │   the likely root cause AND recommends the
│                        │   recovery action itself - weighing confidence
│                        │   and amount at risk, not a fixed lookup table.
│                        │   Rule-based diagnose.py kept for comparison.
└──────────┬─────────────┘
           │ diagnosed causes + recommended actions
           ▼
┌────────────────────────┐
│  recover.py            │   Executes the ACTION RECOMMENDED BY THE LLM
│  (Step 4)              │   (retry_later / prompt_update / delay_retry /
│                        │   manual_flag), falling back to a safe rule-
│                        │   based mapping only if the LLM's action is
│                        │   missing/invalid. Simulates the outcome using
│                        │   realistic, action-specific success rates.
│                        │   Retries are capped (stopping rule) and only
│                        │   apply where retrying is plausible.
└──────────┬─────────────┘
           │ recovery results
           ▼
┌────────────────────────┐
│  validate.py           │   Compares results against the ground-truth
│  (Step 5)              │   answer key: recall (did we catch every real
│                        │   event, including the deliberately ambiguous
│                        │   one?), diagnosis accuracy, and precision
│                        │   (based on genuine time/issuer overlap with
│                        │   a real event, not just label matching).
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────┐
│  run_pipeline.py       │   Runs the full chain end-to-end, prints a
│  (Step 6)              │   human-readable report, generates a plain-
│                        │   English incident summary (2nd LLM call),
│                        │   and saves a full audit trail (detected ->
│                        │   diagnosed -> action -> outcome) to
│                        │   data/audit_trail.json.
└────────────────────────┘
```

---

## How to Run

```bash
pip install -r requirements.txt
```

Create a `.env` file with your free Gemini API key (get one at
aistudio.google.com/apikey - no credit card required):
```
GEMINI_API_KEY=your-key-here
```

```bash
python generate_data.py     # generates synthetic data + ground truth
python run_pipeline.py      # runs the full detect->diagnose->recover->validate pipeline
```

Individual stages can also be run separately: `detect.py`,
`diagnose_llm.py` (AI diagnosis + action), `diagnose.py` (rule-based,
for comparison), `recover.py`, `validate.py`, `compare_diagnosis.py`.

---

## Results (on synthetic ground-truth dataset)

Dataset: 4,000 transactions over 8 days, with 4 deliberately injected
events: an issuer outage, a scattered card-expiry cluster, a month-end
funds-shortage cluster, and a **genuinely ambiguous cluster** (evenly
mixed failure codes with no dominant pattern - the correct diagnosis
here is honest uncertainty, not a forced guess) - plus realistic
background noise (~5% baseline failure rate).

| Metric | Result |
|---|---|
| Recall (real events caught) | 4 / 4 = **100%** (consistent across every test run) |
| Diagnosis accuracy (of events caught) | 4 / 4 = **100%** (consistent across every test run) |
| Precision (flags that overlapped a real event) | 8 / 10 = **80%** (consistent across every test run) |
| Amount recovered | **Rs.74,600 - 84,900** (39-45%) across repeated runs |

We report a range rather than a single cherry-picked run for the
recovery amount, since the LLM's exact confidence/action calls have
some natural run-to-run variance (this is expected behavior for an
LLM-based reasoning step, not instability in the system). What's
notable is that recall, diagnosis accuracy, and precision are
**perfectly stable across every run we tested** - the variance is
confined entirely to the downstream recovery simulation, not the core
detection/diagnosis logic.

**The ambiguous-cluster stress test:** we deliberately injected one
event with no dominant failure code (an even 3-way split across
issuer_timeout, card_expired, and insufficient_funds), where the
objectively correct diagnosis is "unknown." The agent correctly
identified it as such rather than pattern-matching to a majority label
- from the actual run: *"Failures are evenly scattered across expired
cards, timeouts, generic declines, and insufficient funds... manual
investigation is required."* This is direct evidence the agent's
uncertainty is genuine, not just decoration.

**Honest limitation:** 2 of 10 flagged anomalies were false positives -
statistically unlikely but ultimately random noise, correctly diagnosed
as "unknown" and routed to manual review rather than forced into an
incorrect automated action.

### Recovery Rate vs. Risk Tolerance

The diagnosis layer doesn't just classify the cause - it also
**recommends the recovery action itself**, weighing the diagnosed
cause, its own confidence, AND the amount of money at risk. This
produces a real, deliberate trade-off worth stating explicitly:

| Configuration | Recovery Rate | Amount Recovered | Notes |
|---|---|---|---|
| Aggressive (fixed action-per-cause, no amount awareness) | 55.0% | Rs.77,657.18 | Acts automatically whenever a cause is diagnosed, regardless of stakes |
| Risk-adjusted (LLM weighs amount + confidence before acting) | 39.2% | Rs.74,674.71 | Same underlying diagnoses, but defers to manual review on genuinely ambiguous, high-stakes cases |

We use the **risk-adjusted version as our default**. In a real payments
system, an automated wrong action on a large amount is more costly than
a delayed manual review. Concretely, in our final run: two anomalies
where 75-100% of failures were unexplained `generic_decline` codes
(amounts Rs.16,547 and Rs.25,928) were correctly routed to manual
review rather than guessed at - there simply isn't enough signal in a
generic decline to justify an automated action at any confidence
threshold. This is a deliberate safety-over-aggressiveness choice, not
a limitation - the risk threshold could be tuned toward either extreme
depending on a business's actual risk appetite, and both configurations
are available in this repo (`diagnose.py` for the fixed/aggressive
rule-based version, `diagnose_llm.py` for the risk-adjusted LLM
version).

### Auto-Generated Incident Summary

After each run, a second small LLM call turns the full results table
into a plain-English paragraph for a non-technical reviewer - the same
information a payments-ops manager would want, without needing to read
a table. Example from an actual run:

> "The automated recovery agent recently reviewed 10 payment issues and
> successfully recovered Rs.74,674.71 out of Rs.190,584.86 at risk. Its
> largest single success was recovering Rs.25,701.03 during a temporary
> bank outage. The agent automatically resolved 5 clear-cut cases and
> routed the remaining 5 cases to human staff for review because they
> involve unusual patterns requiring human judgment rather than
> automated guesswork."

This costs one additional API call per run (2 total: diagnosis +
summary), and has a safe templated fallback if the call fails.

### Rule-Based vs. LLM Diagnosis

`compare_diagnosis.py` runs both approaches on the same flagged
anomalies side-by-side. In our test runs, cause classification agreed
100% of the time - but the LLM version additionally provides calibrated
confidence and a risk-adjusted action recommendation with a natural-
language justification, which the rule-based lookup table cannot do.

---

## Design Decisions Worth Noting

- **The LLM diagnoses AND decides the action** - not a rule-based
  lookup table. This is the core "agent" behavior: the model reasons
  about ambiguous evidence and makes a bounded decision, the same way
  a human analyst would, rather than following a rigid if/else tree.
- **Amount-aware, risk-adjusted confidence:** the prompt explicitly
  instructs the LLM to be more conservative on high-amount anomalies -
  preferring `manual_flag` over an automated action when the pattern
  is even slightly ambiguous and money at risk is large. This produces
  a real, measured trade-off (55.0% aggressive vs. 39.2% risk-adjusted
  recovery rate) - see "Recovery Rate vs. Risk Tolerance" above.
- **Batched into a single API call:** early versions called the LLM
  once per anomaly (8+ calls), which exhausted the free-tier daily
  quota (20 requests/day) almost immediately. All anomalies are now
  diagnosed in ONE batched prompt/response, keeping this sustainable
  on a free API tier indefinitely.
- **Statistical significance over fixed thresholds (detection):** a
  fixed success-rate cutoff produced 38 false positives on 4,000
  transactions - mostly small-sample noise. Switching to a binomial
  significance test (p < 0.01) cut this dramatically while still
  catching every real event with very high confidence.
- **Bounded, cause-specific retries (recovery):** only `retry_later`
  gets multiple attempts (capped at 3) - other actions get one
  realistic outcome roll, since repeatedly "asking" a customer to
  update a card doesn't improve with more attempts.
- **Low-confidence or high-stakes diagnoses default to `manual_flag`**
  rather than guessing - this is the audit trail's honest exception
  list in practice, validated concretely by the ambiguous-cluster
  stress test above.
- **Validation precision must be based on real overlap, not label
  matching:** an early version of `validate.py` computed precision by
  checking whether a diagnosed label happened to be *any* real cause
  in the dataset - this silently broke once "unknown" became a
  legitimate true cause (for the ambiguous-cluster event), since it
  would credit noise-flagged windows just for saying "unknown." Fixed
  to require genuine issuer + time-window overlap with a real event.

---

## Project Structure

```
payment-recovery-agent/
├── generate_data.py       # Step 1: synthetic data + ground truth
├── detect.py               # Step 2: anomaly detection (statistical)
├── diagnose_llm.py           # Step 3: AI diagnosis + action recommendation
├── diagnose.py                 # Step 3 (rule-based version, for comparison)
├── recover.py                    # Step 4: executes recommended action, simulates outcome
├── validate.py                     # Step 5: ground-truth validation
├── run_pipeline.py                   # Step 6: full pipeline + audit trail
├── compare_diagnosis.py                # rule-based vs LLM side-by-side comparison
├── generate_report.py                    # generates the HTML dashboard (data/report.html)
├── requirements.txt                        # pip dependencies
├── .env                                      # GEMINI_API_KEY (not committed - see .gitignore)
├── .gitignore
├── data/
│   ├── transactions.csv
│   ├── ground_truth_events.csv
│   ├── audit_trail.json
│   ├── pipeline_results.csv
│   ├── validation_metrics.json
│   ├── incident_summary.txt
│   ├── diagnosis_comparison.csv
│   ├── report.html                           # open this in a browser
│   └── backup/                                # auto-backup of last good run
└── README.md
```

---

## Scalability

This prototype is built for a batch of a few thousand transactions and
a handful of daily anomalies - appropriate for a hackathon demo, but
several architectural choices would need to change for Razorpay's
actual transaction volume (crores of transactions/day):

- **Batch to streaming:** the pipeline currently loads a full CSV and
  processes it once. Real-time detection at scale would need a
  streaming architecture (e.g., Kafka + a stream processor) instead of
  a periodic batch job, so anomalies are caught within minutes, not at
  the next scheduled run.
- **LLM batching has a ceiling:** we batch all flagged anomalies into
  one prompt to minimize API calls - this works well for a handful of
  events, but would hit token limits and degrade in quality with
  hundreds/thousands of anomalies. At scale, this needs chunking into
  smaller batches, parallel calls with proper rate-limit handling, and
  likely caching for repeated failure patterns to avoid redundant calls.
- **Free-tier quota is a hackathon constraint, not a production plan:**
  a real deployment needs a paid tier (or self-hosted model) with
  capacity planning, plus multi-provider redundancy so a single
  vendor's outage doesn't stall recovery.
- **Flat files don't scale:** `data/transactions.csv` and
  `audit_trail.json` work for a demo; production needs a proper
  time-series store for transactions and a structured logging/
  observability stack for the audit trail, both queryable at scale.
- **Single-threaded to distributed:** detection, diagnosis, and
  recovery could become independent services connected by a queue,
  so each stage scales independently based on its own load (e.g.,
  detection is cheap and high-volume; diagnosis is LLM-bound and
  needs its own throughput management).

What's already scale-appropriate by design: only calling the LLM on
*flagged* anomalies (not every transaction) keeps cost proportional to
actual risk, not raw volume - this cost-consciousness would carry
forward into a production version unchanged.

## Data Privacy

The diagnosis layer sends **only aggregated, anonymized statistics** to
the third-party LLM (Gemini) - never individual customer or transaction
identifiers. Concretely, for each flagged anomaly, the API call
includes just: issuer name, time window, a count of failures per
failure code, and a total amount. It never includes `customer_id`,
`txn_id`, or any single transaction's details. This aligns with GDPR's
data minimization principle - the model reasons about failure
*patterns*, not identifiable people.

What a production deployment would additionally need to fully address
GDPR compliance (out of scope for this prototype, noted honestly):

- **Data residency** - confirming where the LLM provider processes
  requests, and using a regional endpoint or private model if required
  for EU/India data residency rules.
- **Right to erasure** - a retention/deletion policy for
  `data/transactions.csv` and the audit trail if a customer requests
  their data be removed (helped by the fact that our audit trail
  already excludes `customer_id`).
- **Purpose limitation** - ensuring the LLM provider's data-usage terms
  restrict processing to the stated purpose (payment diagnosis) only.
- **Legal basis for processing** - typically covered under "legitimate
  interest" for fraud/recovery purposes, but this is a legal
  determination for a real deployment, not something code alone
  resolves.

## Testing

Each component was tested individually before being wired into the
full pipeline, and again as part of the end-to-end run:

- **`generate_data.py`** - verified the ambiguous-cluster injection
  produces a genuinely balanced failure-code mix (no code exceeding
  ~33% of failures) rather than an accidentally dominant one.
- **`detect.py`** - verified against known ground truth that both real
  outage windows are flagged with very high statistical significance
  (p ~ 1e-8 to 1e-9), and tested two alternative thresholding
  approaches (a stricter fixed cutoff, and a formal Benjamini-Hochberg
  correction) - both were rejected after confirming they reduced
  recall on the card-expiry event, and that decision is documented in
  Limitations below rather than silently discarded.
- **`diagnose_llm.py`** - tested resilience to two real failure modes
  encountered during development: temporary server unavailability
  (503 errors, handled via retry with backoff) and daily free-tier
  quota exhaustion (429 errors, handled by falling back to a second
  model with a separate quota pool). Both were verified working via
  live runs, not just written defensively.
- **`recover.py`** - the action-resolution fallback logic (using the
  LLM's recommended action, falling back to a safe rule-based mapping
  if that response is ever missing or invalid) was unit-tested with
  synthetic rows covering all three cases: valid action present,
  action missing, and an invalid/unrecognized action string.
- **`validate.py`** - the precision-calculation logic was corrected
  after testing revealed a bug: an early version credited any
  "unknown"-labeled flag as a true positive once "unknown" became a
  legitimate ground-truth cause (for the ambiguous-cluster event),
  which would have silently inflated precision. Fixed to require
  genuine issuer + time-window overlap with a real event instead.
- **Full pipeline** - run end-to-end multiple times; final validated
  run: 4/4 recall, 4/4 diagnosis accuracy, 8/10 precision,
  Rs.74,674.71 recovered of Rs.190,584.86 at risk (see Results above).

## Limitations & Future Work

- **Detection cannot catch scattered (non-time-clustered) patterns.**
  Our detection layer identifies anomalies via concentrated time-window
  statistics, which works well for issuer outages and payday-clustered
  funds shortages, but structurally cannot detect a pattern like card
  expiry, which is deliberately scattered across random customers/times
  with no time or issuer concentration. In this implementation, the
  card-expiry event is only "caught" via an incidental flagged window
  that happens to share its dominant failure code - not genuine
  spatial/temporal detection. A production system would need a
  separate detection strategy for non-clustered patterns (e.g., a
  periodic full-dataset scan for failure-code prevalence, independent
  of time windowing). We investigated tightening the detection
  threshold (both a stricter fixed p-value and a formal
  Benjamini-Hochberg false-discovery-rate correction) to reduce noise,
  but confirmed via testing that both approaches drop recall on this
  event from 4/4 to 3/4 - so we kept the original threshold and are
  documenting this honestly rather than hiding it.
- **Thresholds were tuned against a single fixed random seed.** Our
  synthetic data generator uses `random.seed(42)` for reproducibility,
  which means every threshold decision in this repo (detection
  significance level, the LLM prompt's numeric confidence/amount
  rules) was validated against one specific dataset instance. This is
  a real overfitting risk - a different seed could expose numbers that
  don't generalize. Future work: validate thresholds across multiple
  seeds before treating them as final.
- Recovery outcomes are simulated using assumed success rates, not real
  gateway retry data - a production version would calibrate these from
  historical data.
- Detection currently uses fixed 2-hour windows; adaptive window sizing
  could catch shorter or longer-duration anomalies more precisely.
- The free-tier Gemini API has a 20-requests/day limit per model and
  occasional server unavailability; the pipeline handles both
  gracefully (retry with backoff, then falls back to a second model
  with a separate quota pool), but a production deployment would use a
  paid tier for reliability at scale.