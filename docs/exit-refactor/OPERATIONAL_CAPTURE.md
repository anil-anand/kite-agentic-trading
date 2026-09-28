# Authentic operational capture

Historical replay and synthetic tests do not qualify as operational observations.
These adapters collect new observations from the running engine or an authorized
external real-time feed. They do not enable candidate live dispatch, connect a
broker, or read credentials. An injected fixture clock or DEV runtime marks the
entire capture `SYNTHETIC_FIXTURE`, which fails the empirical gate.

## Live shadow

Before starting the backend, create a nonsensitive JSON capture plan outside the
application runtime storage:

```json
{
  "directory": "/absolute/path/to/new/capture-directory",
  "study_id": "registered-study-id",
  "mode": "LIVE_SHADOW",
  "data_source_id": "approved-feed-identifier",
  "source_revision": "source_tree_sha256-from-registration",
  "policy": {"policy_version": "deterministic-exit-v1"}
}
```

Use the complete candidate policy from the frozen registration if any defaults
were changed. `policy` is restored and pinned before capture begins. The directory
must not exist. Set `KITE_EXIT_CAPTURE_PLAN` to the absolute plan path when launching
the backend. Only already-authorized shadow evaluations are observed; this setting
does not switch a position from legacy control to shadow or enable entries.

The existing engine records decisions only after their durable journal commit.
A bounded background writer captures receipt/persistence wall times and exact
position-epoch, intent, order-attempt and fill joins. The risk thread does not
perform recorder disk I/O. Overflow, failed observation or an unclean shutdown
leaves the capture ineligible. Graceful process shutdown seals the capture.
Acquisition writes full observations directly to the append-only file and retains
only the latest event and a decision-identity index in memory. Duplicate checks
do not scan earlier observations. Shutdown flushes and seals without constructing
the full report; explicit offline export materializes it after validating every
record and the complete hash chain. Allow disk space for the complete decision
snapshots; repeated inputs are retained for independent replay.

Export retained data after the process closes:

```bash
uv run python -m backend.backtesting.operational_capture \
  --directory /absolute/path/to/capture-directory \
  --output /absolute/path/to/new-shadow-export.json
```

Reopened captures are read-only. Restart with a new directory; a restart cannot
silently bridge an unobserved interval. Hash chains verify retained content,
not the authenticity of a data vendor or operator attestation.

## Isolated real-time paper session

Use a plan with the same identity fields, `mode: "ISOLATED_PAPER"`, no `directory`
field, and optional `initial_capital`, `daily_loss_limit`, `execution_policy`,
`context_policy`, `session_policy`, and `maximum_feed_lag_seconds` (default 30,
maximum 300). Policy/execution settings remain fixed throughout the session.

```bash
approved-feed-exporter | uv run python -m backend.backtesting.paper_session \
  --plan /absolute/path/to/paper-plan.json \
  --directory /absolute/path/to/new-paper-directory \
  --output /absolute/path/to/new-paper-export.json
```

The feed emits UTF-8 JSON lines:

- `{"type":"history","symbol":"ABC","candles":[...]}`: completed causal
  warmup bars, before that symbol's entry. These are feature history and do not
  become operational observations.
- `{"type":"entry","signal":{"tradingsymbol":"ABC","direction":"BUY",
  "entryPrice":100,"stopLoss":95,"strategy":"approved-strategy"},
  "quantity":10,"quote_price":100,"quote_observed_at":"aware-ISO-time"}`:
  create a paper entry at a fresh externally observed quote. The signal may
  include its complete original market context and `effective_config` is an
  optional top-level field. The recorded thesis is frozen before simulated
  execution and bound to its actual paper fill.
- `{"type":"candle","symbol":"ABC","candle":{"date":"aware-bar-start",
  "open":100,"high":101,"low":99,"close":100,"volume":1000}}`: a newly
  completed five-minute bar. Receipt and availability times come from the
  recorder wall clock. Old/future bars and revisions are rejected.
- `{"type":"clock"}`: optional independent risk tick. The CLI also generates
  one every second when the feed is silent.

Each symbol has one position epoch in a paper session. The adapter evaluates the
shared candidate policy and sends simulated mutations through the shared order
coordinator. Acknowledgements never become fills. End-of-input preserves and
censors residual exposure and working orders. It does not invent a closing fill.
Paper sessions also spool execution results and account marks to disk. They keep
only the latest runner diagnostic and compact exact position/intent/order joins
in memory. Full offline candidate and portfolio reports retain their existing
history behavior. The paper export remains the complete retained observation
history, including every HOLD and every pending-exit decision.
Execution-to-decision joins are written once per result and reconstructed during
export; repeated intent observations do not rewrite a growing list of earlier
decision IDs. The final intent and its final observation contain the complete
join, while intermediate observations retain their contemporaneous deltas.
This entry feed is a declared operational experiment; production entry-selection
parity is established by the separate full-portfolio study.

The resulting `operational-run-v1` JSON is supplied to the registered acceptance
workflow's `record-operational` command. Actual observations must meet the frozen
sample, timing, parity and reconciliation criteria. Running a command alone is
not sufficient evidence.

## Multiple sessions and restarts

Before registering the study, declare its complete cohort in the study plan:

```json
{
  "operational_cohorts": {
    "ISOLATED_PAPER": {
      "required_slots": [
        {"slot_id": "paper-01", "session_dates": ["2026-09-29"]},
        {"slot_id": "paper-02", "session_dates": ["2026-09-30"]}
      ]
    }
  }
}
```

This fragment illustrates the format; declare enough actual session dates to meet
the study's frozen minimum, and declare the separate `LIVE_SHADOW` cohort too.
Before each capture starts, consume an attempt:

```bash
uv run python -m backend.backtesting.acceptance claim-operational \
  --directory /absolute/path/to/registered-study \
  --mode ISOLATED_PAPER --slot paper-01
```

Copy the returned `study_id`, `mode`, `source_revision`, `policy`, `capture_slot`
and `capture_attempt_id` into the capture plan with its `data_source_id`. The
claim response also includes receipt/administrative fields; do not copy those
extra fields into the recorder's identity plan. Keep each capture directory new.
Import each completed export using `record-operational` as above.

Every claim and report is immutable. A restart creates another claim and capture;
it does not overwrite its predecessor. Missing claimed exports, failed captures,
unresolved positions, duplicate live execution identities and overlapping captures
remain failures. Sample counts use distinct observed session dates and all retained
captures; enough individually small captures can satisfy the aggregate minimum.
Failed attempts cannot be discarded to obtain a passing cohort. Historical exports
without the original claimed identity cannot be relabelled as new cohort captures.

Hashes and the complete local claim inventory establish consistency within this
workflow. They cannot authenticate a vendor, an external clock, or undisclosed
captures created outside it. Independent provenance review remains necessary.
Existing registrations with one continuous export per mode remain readable.
