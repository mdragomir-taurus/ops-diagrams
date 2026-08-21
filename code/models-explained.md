# FCP Models — What They Actually Are

| | |
|---|---|
| **Audience** | Infrastructure / platform engineers with no ML background |
| **Date** | 2026-07-28 |
| **Purpose** | Explain what each model does, how they differ, and what FMS / FMDS mean |

---

## 1. The one ML concept you need

Every model here does the same mechanical thing:

> take a transaction → turn it into a list of numbers → return **one number between 0 and 1**.

That number is a **score**, not a decision. A score of `0.87` does not mean "87 % fraud" and it does not mean "block". It is an ordering: higher = more like the fraud the model was trained on.

The score becomes a decision only when someone compares it to a **threshold**. Thresholds are a business choice, not a model property — this is why they are versioned alongside the model and why they show up in configuration:

```python
# boss-caf-ml-models/src/models/tenured/tenured_predictor.py
self.LOW_THRESHOLD  = 0.0165
self.HIGH_THRESHOLD = 0.034
```

Three terms that will come up constantly:

| Term | Meaning | Infra consequence |
|---|---|---|
| **Feature** | one input number (e.g. "transactions in the last 24 h") | features come from somewhere — MongoDB, S3, an API. That is a runtime dependency |
| **Threshold** | the cut-off that turns a score into a decision | changing it changes business behaviour without changing the model |
| **Artifact** | the trained model file (`.pkl`, `.cbm`, `.joblib`) | today mostly baked into the Docker image — the thing we want to change |

**The asymmetry that governs everything:** approving a fraudulent transaction costs real money; declining a legitimate one costs a customer. Fraud is rare — a fraction of a percent. So a model that always says "not fraud" is ~99.5 % accurate and completely useless. This is why the training code optimises *missed fraud dollars subject to a false-positive cap* rather than accuracy, and why comparing two models needs weeks of data rather than an afternoon.

---

## 2. FMS and FMDS

Two acronyms, constantly confused. **They are not a pair, and one is not a version of the other.**

### FMDS — Fraud Model **Data** Service

A **.NET** service (`ASPNETCORE_*` in its values). It is a **data pipeline**, not a model. It contains no ML.

```
trx.transfer.changed  ──►  FMDS  ──►  fmds.transaction.created
   (raw transaction         (enrich,      (transaction ready
    events)                  reshape)       to be scored)
```

Think of it as the ETL step that prepares the input. When FMDS is down, models get no work — but nothing is scored incorrectly.

### FMS — Fraud **Modeling System**

The legacy family of Python fraud models, from the repo `boss-caf-fraud-model`, image `fraud-model-service`. Two variants run in prod: **tenured** and **non-tenured**.

They work **asynchronously**:

```
Kafka fmds.transaction.created
   │  each consumer filters on the "flow" header
   ├─► FMS tenured      (consumer group FMS-tenured)
   └─► FMS non-tenured  (consumer group FMS-non-tenured)
              │
              └─► SNS fraud-model-service-output-prod
```

Nobody waits for an HTTP response. That is why adding a shadow challenger is nearly free — a new consumer group reads the same stream independently.

> **Naming trap:** `boss-caf-fraud-model` (the FMS runtime, Python, Kafka→SNS) and `boss-caf-ml-models` (the synchronous FastAPI service) are different repositories with overlapping model names. `fms-model-tenured` in prod runs the **former**.

---

## 3. Tenured vs non-tenured

The single most important split, and it is not about the algorithm — it is about **how much history the customer has**.

| | Tenured | Non-tenured |
|---|---|---|
| Customer | has transaction history | new, little or no history |
| Available features | behavioural: averages, deviations from the customer's own norm, velocity | mostly static: device, IP, card age, geography |
| Fraud pattern | account takeover, behaviour change | synthetic identity, stolen card |
| Model | trained only on tenured customers | trained only on new customers |

One model for both would be worse at each: for a tenured customer the strongest signal is "this is unlike *their* usual behaviour", which does not exist for a new customer. So there are two models, and the router is the `flow` header (async) or the `tenured: bool` field on the sender (sync).

`AGE_OF_CUSTOMER_DAYS`, `TOTAL_AMOUNT_MEAN_30D`, `TOTAL_AMOUNT_Z_SCORE_7D` in the tenured feature list are exactly this: the z-score says "how many standard deviations from this customer's own average is this amount".

---

## 4. The models, one by one

### 4.1 Fraud models

| Model | Question it answers | Payment type | Transport |
|---|---|---|---|
| **fms-model-tenured** | Is this transaction by an existing customer fraudulent? | card / general | async, Kafka → SNS |
| **fms-model-non-tenured** | Is this transaction by a new customer fraudulent? | card / general | async, Kafka → SNS |
| **ach-model** | Is this **bank transfer** fraudulent? | ACH (bank account) | sync, HTTP via Terminus |
| **new-customers-model** | Is this new customer fraudulent? | card | sync, HTTP via Terminus |

**Why ACH is separate.** ACH is a US bank-to-bank transfer. It settles slowly and can be reversed weeks later, and there is no card network doing fraud checks first. Different data (bank account, not card), different fraud window, different signals — hence its own model. Its response also carries `matchScore` — how well the name on the bank account matches the customer's name — which does not exist for cards.

**Why `new-customers-model` and `fms-model-non-tenured` both exist** and both score new customers: they came from different projects at different times, and they sit on different transports. This overlap is a legitimate question to raise with the ML lead — it is not obviously intentional.

### 4.2 Not fraud models at all

| Model | Question | Output |
|---|---|---|
| **pricing-model** | How price-sensitive is this customer? | `score` + `segment: sensitive \| non_sensitive` |
| **mtu-model** | Mobile top-up scoring | `score` |
| **engager** | Customer engagement / propensity | separate service (`ailab/engager-server`) |

Pricing is a **regression**-flavoured model feeding a business segment, and it fails *open*: if the feature API is unreachable it returns `score = None, segment = "non_sensitive"` and the transaction proceeds. A fraud model must never behave like that — hence the open question about fail-closed semantics in the MLOps doc.

Pricing is also the only model that already fetches features from **signature-api** (the Korell/Feast platform) rather than computing them itself. It is the shape everything else is expected to converge on.

---

## 5. The "challenge" cascade in the tenured model

Worth understanding because it is *not* champion/challenger, despite the name.

```python
# tenured_predictor.py — simplified
fm_score = main_model.predict_proba(features)[1]

if 0.0165 <= fm_score < 0.034:            # grey zone
    challenge_score = challenge_model.predict_proba(challenge_features)
    if challenge_score < 0.33:
        fm_score = -fm_score              # negative = flagged by the cascade
        scored_by_challenge_model = True

if is_do_new_cc:
    fm_score = 0.98                       # rule override, bypasses the model entirely
```

Read it as: the main model is confident about clearly-good and clearly-bad transactions. In the narrow band between the thresholds it is unsure, so a **second, specialised model** is consulted and its verdict is used in production.

Three things to notice:

1. **The grey zone is tiny** — between 0.0165 and 0.034. Most transactions never touch it.
2. **The negative score is a signalling convention**, not a probability. Downstream code reads the sign.
3. **Rules can override the model outright** (`is_do_new_cc → 0.98`). The model is one input among several; `GetStrictestRecommendation` in Terminus combines model, rules and adapter, and the strictest wins.

That last point matters for C/C: **a better model does not automatically mean better decisions**, because rules may be overriding it. When comparing models you must look at both the score and the final decision.

---

## 6. How a score becomes a decision (synchronous path)

```
Terminus worker
   └─► model → score
         └─► rule engine (DMN / GRule) → recommendation
               └─► GetStrictestRecommendation([rules, model, adapter])
                     └─► VerificationDecisionV2 → CVS
                           recommendation: accept | challenge | deny | pending
```

`challenge` here means **send the customer to manual/step-up verification** — the third meaning of the word in this codebase. See the terminology table in the C/C plan.

---

## 7. Where model files live today

| | Where | Consequence |
|---|---|---|
| Artifact | **committed to git**, baked into the image (`src/models/tenured/fraud.pkl`, 541 KB; `model.cbm`, 2.7 MB) | new model = new commit + new image + new tag |
| Feature list | `features.pkl`, also in git | the feature contract is not independently versioned |
| Model version | the image tag (`MODELS_VERSION: "2.2.2"` = image tag `2.2.2`) | model version and code version cannot move independently |

This is the main thing the MLOps work is trying to change: the model should be **data pulled at startup**, not code baked into an image. Two ways to get there — teach the app to download from S3, or let KServe's storage-initializer do it. See `docs/mlops-arhictecture-plan.md`.

---

## 8. Quick reference

| Acronym | Expansion | What it is |
|---|---|---|
| **FMS** | Fraud Modeling System | legacy Python fraud models, async Kafka→SNS, repo `boss-caf-fraud-model` |
| **FMDS** | Fraud Model Data Service | .NET data pipeline feeding FMS; no ML |
| **ACH** | Automated Clearing House | US bank-to-bank transfer |
| **CVS** | Customer Verification Service | the caller that asks for a decision |
| **Tenured** | — | customer with transaction history |
| **Non-tenured** | — | new customer |
| **Score** | — | 0..1, an ordering, not a probability of fraud |
| **Threshold** | — | the business cut-off turning a score into a decision |
| **Champion / challenger** | — | two model *versions* compared against each other |
| **Challenge (tenured)** | — | grey-zone cascade to a second model |
| **`WasChallenged`** | — | the customer was sent to manual verification |
