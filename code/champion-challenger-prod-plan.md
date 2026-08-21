# Champion / Challenger on Production — Implementation Plan

| | |
|---|---|
| **Status** | Proposed — for review with staff-dev and ML lead |
| **Date** | 2026-07-28 |
| **Owner** | Georgiy Marinov (Infrastructure) |
| **Priority** | #1 for the FCP platform |
| **Scope** | HAL/SOM prod only. Training, registry and KServe are explicitly out of scope |
| **Related** | `docs/mlops-arhictecture-plan.md` (branch `mlops-pipeline`), `docs/terminus-shadow-scoring-spec.md` |

---

## 1. TL;DR

1. **Traffic splitting is the easy half.** The hard half is knowing which model is better, and nothing in prod produces that data today.
2. **Shadow first, weighted second.** Shadow at 100 % answers "is the challenger better"; the weighted ramp only limits blast radius.
3. For the **FMS models the shadow costs a values.yaml change** — a second Kafka consumer group scores every transaction independently.
4. For the **synchronous models the split belongs in Terminus**, not in the ingress: only Terminus can hash on `customer_id` and record which arm was assigned.
5. **KServe is not on the critical path** — canary is silently ignored in RawDeployment, so it does not solve this problem. **Traefik is not on the path either** — it is not deployed on prod.

---

## 2. Current state (verified 2026-07-28)

### 2.1 Two scoring paths with different mechanics

| | Path A — synchronous | Path B — asynchronous |
|---|---|---|
| Models | `ach-model`, `new-customers-model`, `pricing-model` | `fms-model-tenured`, `fms-model-non-tenured` |
| Transport | HTTP via Terminus worker | Kafka `fmds.transaction.created` → SNS |
| Image source | `caf-ml-models` (`boss-caf-ml-models`) | `fraud-model-service` (`boss-caf-fraud-model`) |
| Decision record | `VerificationDecisionV2` → CVS | SNS message `FraudModelScoreUpdated` |

### 2.2 What already exists

- **`ach-model-challenger`** — a full second Deployment with its own ingress and hostname, **running the same image tag `2.2.2` as the champion**. The plumbing exists; there is nothing to compare.
- **Routing for ACH lives in Terminus** (`external-services/ach-model-api/client_predict.go:62`):
  ```go
  if sc.unleashClient != nil && sc.unleashClient.IsEnabled("UseAchFraudModelChallenger") {
      endpoint = sc.challengerEndpoint
  }
  ```
  This is an **either/or switch**, not champion/challenger: a transaction is scored by one model or the other, never both.
- **`IsEnabled()` is called without an Unleash context** → no per-customer stickiness. Assignment is not reproducible, and a Zeebe **retry can land on a different model than the first attempt**.
- **`new-customers-model` has no routing mechanism at all.** `challengerEndpoint` is assigned in the constructor (`new-customers-model-api/client.go:39,48`) and never read; there is no Unleash client in that service. It is dead code.

### 2.3 Decision tracking that already exists

Richer than expected, but built for operational audit rather than experiment analysis.

`fraudcore.VerificationDecision` carries `Recommendation`, `DecisionDate`, `RuleID`, `Features` (the rule-engine feature snapshot), `DenyOverride`, and `VerificationSources` with per-source attribution (`ACHMODEL`, `RULES`, `ACH`, …) plus `isDecisive` flags. Model version is present inside `Payload.modelVersion`.

The FMS SNS message carries `CPAS_ID`, `SCORE`, `MODEL_VERSION`, `FEATURES`, `SCORED_BY_CHALLENGE_MODEL`, `CHALLENGE_MODEL_SCORE` and the thresholds.

Prometheus (all `PodMonitor`-scraped today):

| metric | labels |
|---|---|
| `models_score_distribution` | `model_name`, `version` |
| `model_inference_latency` | `model_name`, `version` |
| `feature_store_latency` | `model_name`, `version` |
| `decisions_counter` | `productType`, `recommendation` |
| `scoring_engine_duration_seconds` | `scoringEngine` |

### 2.4 Gap analysis

| Needed for C/C | Present | Note |
|---|---|---|
| Model version in the decision | yes | `Payload.modelVersion`, `MODEL_VERSION` |
| Feature snapshot | yes | `Features` / `FEATURES` |
| Source attribution | yes | `VerificationSources` + `isDecisive` |
| **Experiment arm recorded** | **no** | nothing records which arm a transaction was assigned to |
| **Paired scores per transaction** | **no** | on path A exactly one model scores |
| `model_version` on `decisions_counter` | no | deny rate cannot be split by arm |
| **Prediction → outcome join** | **no** | chargebacks are never joined to predictions |

---

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| C1 | **Shadow at 100 % precedes any weighted ramp** | Paired scores on identical transactions remove cohort noise; full label coverage; zero business risk. A 5 % arm needs ~10× longer to reach the same statistical power |
| C2 | **The weighted ramp is a blast-radius control, not a measurement** | By the time the ramp starts the answer must already be known from shadow |
| C3 | **Arm assignment is computed in Terminus, never in the proxy** | The key is `customer_id`, which lives in the JSON body — no ingress can hash on it. Terminus also records the assignment |
| C4 | **Assignment must be deterministic, sticky and recorded** | Zeebe retries must not change the arm; intent-to-treat analysis requires the assignment, not only the outcome |
| C5 | **Keep nginx; do not couple this to the Traefik migration** | Traefik runs only on `dev-hal` (`c-8zt68`), for Zeebe L4 — not on prod. The org `service` chart v0.16.0 emits no Traefik CRDs |
| C6 | **KServe stays off the critical path** | `canaryTrafficPercent` is silently ignored in RawDeployment ([kserve#5335](https://github.com/kserve/kserve/issues/5335)) |
| C7 | **Adopt versioned backend Services (staff-dev Approach 2)** | Makes the future Go-router cutover mechanical rather than a rewrite |
| C8 | **A written promotion/rollback rule is agreed before the ramp starts** | Otherwise the ramp becomes an argument about interpreting graphs |

---

## 4. Phase 1 — Shadow

### 4.1 FMS models — free, values.yaml only

Both FMS consumers already read the **same** topic `fmds.transaction.created` and filter on the `flow` header; they are separated only by `KAFKA_CONSUMER_GROUP`. A challenger with its own consumer group therefore reads the same stream independently and scores **every** transaction.

```yaml
fms-model-tenured-challenger:
  image:
    repository: 101838717447.dkr.ecr.us-east-1.amazonaws.com/fraud-model-service
    tag: "<challenger-version>"           # MUST differ from champion
  env:
    - { name: KAFKA_CONSUMER_GROUP, value: "FMS-tenured-challenger" }
    - { name: MODEL_VERSION,        value: "<challenger-version>-tenured" }
    - { name: OUTPUT_SNS_ARN,       value: "arn:aws:sns:us-east-1:101838717447:fraud-model-service-output-challenger-prod" }
    - { name: MODEL_TENURED,        value: "true" }
```

Properties:

- 100 % shadow, **no code change**;
- zero risk — a separate consumer group does not touch the champion's offsets;
- paired scores on identical transactions, joinable by `CPAS_ID`;
- `models_score_distribution{model_name, version}` splits by version in Prometheus automatically.

**Output routing** — pick one:
1. a separate SNS topic (cleanest, recommended); or
2. the same topic plus `MessageAttributes: {ModelRole: challenger}` and a subscription filter policy so the production subscriber never sees challenger messages.

> Do **not** publish challenger results to the production SNS topic without a filter — downstream consumers would see two messages per transaction.

**Prerequisite:** MSK capacity review — a second consumer group doubles the read throughput on that topic.

### 4.2 Synchronous models — dual call in Terminus

The champion is called synchronously as today; the challenger is called asynchronously and its response is discarded. Full specification: **`docs/terminus-shadow-scoring-spec.md`**.

Non-negotiable properties: the challenger call never blocks the response, never affects latency, uses its own connection pool and a hard timeout; failures increment a metric and nothing else.

### 4.3 Exit criteria for Phase 1

- ≥ 2 weeks of paired scores, **and** enough resolved chargebacks to compare (the count follows from the real fraud rate — see §7);
- challenger latency p95 and error rate within budget;
- an offline report: ROC / precision-recall on the shared population, disagreement rate, missed-fraud $ and false-positive $ at the challenger's proposed threshold.

---

## 5. Phase 2 — Weighted ramp

Only after Phase 1 produces a positive result.

### 5.1 Topology (staff-dev Approach 2, adopted)

- `new-customers-model` — champion, versioned backend Service.
- `new-customers-model-v{X}` / `-challenger` — challenger, versioned backend Service.
- One stable host; the canary Ingress targets the challenger Service.
- When the Go router lands it becomes the single Service behind the stable host and fans out to the same versioned Services; the canary Ingress is deleted.

### 5.2 Routing — `canary-by-header`, not `canary-weight`

```yaml
# canary Ingress
nginx.ingress.kubernetes.io/canary: "true"
nginx.ingress.kubernetes.io/canary-by-header: "X-Model-Arm"
nginx.ingress.kubernetes.io/canary-by-header-value: "challenger"
```

```go
// Terminus — deterministic, sticky, recorded
uctx := unleashcontext.Context{UserId: profileID}
if unleashClient.IsEnabled("UseNewCustomersModelChallenger", unleash.WithContext(uctx)) {
    req.Header.Set("X-Model-Arm", "challenger")
    arm = "challenger"
}
```

Why not `canary-weight`:

| Problem with `canary-weight` | Fixed by `canary-by-header` |
|---|---|
| random per request → a Zeebe **retry can hit a different model** | hash of `profileID` — the same transaction always lands in the same arm |
| the assignment is not recorded | Terminus knows the arm before the call and writes it down |
| percentage lives in Git → rollback needs a PR + ArgoCD sync | Unleash percentage changes instantly, including to 0 % |
| nginx allows only **one** canary per stable Ingress | header-based routing extends to three or more arms |

With Unleash `flexibleRollout` + `stickiness=userId`, the percentage is a normalised MurmurHash of `(groupId + userId)` → 0..100: deterministic, stable across pods and restarts.

nginx keeps a purely topological role — one host, several versioned backends — which is exactly the role the Go router takes over later.

### 5.3 Ramp

`0 % → 5 % → 10 % → 25 % → 50 %`, each step held long enough for the fast guardrails to be meaningful, with an explicit go/no-go before each increase.

---

## 6. What has to be built regardless of phase

### 6.1 Prediction event (new — infrastructure work)

Neither path produces a per-transaction record that we own and can join to outcomes.

```jsonc
{
  "eventId":      "uuid",
  "transactionId": "CPAS_ID / externalId",
  "assignmentKey": "profileId",     // what the hash was computed on
  "arm":           "champion|challenger|shadow",
  "rolloutPct":    5,
  "model":         "new-customers",
  "modelVersion":  "2.3.1",
  "score":         0.87,
  "decision":      "accept|challenge|deny",
  "featureHash":   "sha256:…",
  "ts":            "RFC3339"
}
```

**PCI constraint:** the event carries identifiers, scores and a feature *hash* — never the raw request body. The KServe payload logger was rejected for exactly this reason: it would ship full PAN and PII (`CARD_NUMBER`, `CARD_CVV_AUTHORIZATION_CODE`, `SENDER_*`) into Kafka.

Open: target Kafka (MSK prod vs on-prem), partitions, retention, JSON vs Avro, consumer owner. Tracked in the MLOps doc §10.

### 6.2 Outcome join

`prediction event → ODS/RTDW ← chargebacks`, joined on `transaction_id`. Without this the ramp is blind.

### 6.3 Metric additions

- `model_version` label on `decisions_counter` — deny rate per arm in Grafana.
- Grafana dashboard: score distribution per version, disagreement rate, per-arm deny rate, challenger error/timeout rate.

---

## 7. Guardrails and the promotion rule

| Horizon | Signal | Action |
|---|---|---|
| seconds | p95/p99 latency, error rate, timeout rate, PSI of the score distribution | **automatic rollback** of the arm to 0 % |
| hours | deny rate, manual-review queue volume, agent override rate | alert, human decision |
| days–weeks | chargeback rate, missed fraud $, false positive $ | promote or reject the challenger |

Two statistical rules to agree **before** the ramp:

1. **Sequential monitoring.** Metrics will be watched continuously, so a fixed-horizon t-test is invalid. Use SPRT or always-valid confidence sequences, or pre-register the decision points.
2. **Selective labels.** A transaction the challenger would have blocked has no outcome label. Comparisons are made on the population the champion approved, and the caveat is stated in the report.

---

## 8. Work breakdown

| # | Item | Owner | Depends on |
|---|---|---|---|
| W1 | `fms-model-tenured-challenger` — consumer group, SNS topic, values (hal + som) | Infra | challenger image tag from DS |
| W2 | MSK capacity check for the second consumer group | Infra | — |
| W3 | Terminus shadow dual-call (see spec) | Terminus dev | W5 |
| W4 | Versioned Services + canary Ingress for `new-customers-model` | Infra + staff-dev | — |
| W5 | Prediction event topic + schema | Infra | Kafka decision |
| W6 | Consumer → ODS/RTDW + chargeback join | Infra + data | W5 |
| W7 | `model_version` label on `decisions_counter` | Terminus dev | — |
| W8 | Grafana C/C dashboard | Infra | W1, W5 |
| W9 | Written promotion/rollback rule | ML lead + CAF business | — |

**Shortest path to first data: W1 + W2.** No code, no risk, and it produces paired scores for the FMS models immediately.

---

## 9. Non-goals

- Deploying KServe to prod — decided separately, on its own merits (see the MLOps doc).
- Migrating prod ingress to Traefik — MTNCS-4559, independent schedule. Traefik's asynchronous mirroring is a genuine argument *for* that ticket, but must not block this work.
- Changing how models are packaged or trained.

---

## 10. Terminology warning

The word "challenge" already means three different things in this codebase. Fix the vocabulary before the first meeting:

| Term | Meaning |
|---|---|
| **challenge** (tenured model) | grey-zone cascade: if the score falls in `[0.0165, 0.034)` a second specialised model is consulted **synchronously and its verdict is used** |
| **challenger** (this document) | a competing model *version* evaluated against the champion |
| **`WasChallenged`** (Terminus) | the customer was sent to a manual step-up verification |

---

## 11. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Which model version becomes the first challenger, and for which model? | ML lead |
| 2 | Real fraud rate and transaction volume — needed to size the shadow period | ML lead / data |
| 3 | Kafka for the prediction event: MSK prod or on-prem? | Infra + team |
| 4 | Do ACH (Unleash) and new-customers (canary Ingress) converge on one mechanism, and when? | staff-dev |
| 5 | Who owns the offline comparison report? | ML lead |
| 6 | Fail-closed vs fallback when a model is unavailable — business decision | CAF business |

---

## 12. Findings worth fixing outside this plan

1. **`ach-model-challenger` runs the same image tag `2.2.2` as the champion** — there is nothing to compare until it is given a different model.
2. **`new-customers-model-api` `challengerEndpoint` is dead code** — remove it or wire it up.
3. **Bug in `boss-caf-ml-models`**, `src/models/tenured/tenured_predictor.py:65`:
   ```python
   if challenge_score < self.self.LOW_CHALLENGE_THRESHOLD:   # self.self → AttributeError
   ```
   Reached only on the grey-zone path. Not currently in prod (prod tenured runs `fraud-model-service` from `boss-caf-fraud-model`), but it will fire the moment tenured is served from this repo or from the KServe transformer.
4. **Dead MLflow configuration** in every KServe manifest on the `kserve` branch (`MODELS_MLFLOW_TRACKING_URI`) — ClearML superseded it.
