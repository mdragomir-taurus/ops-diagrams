# Terminus — Shadow Scoring for Champion / Challenger

| | |
|---|---|
| **Status** | Draft spec — for the Terminus team |
| **Date** | 2026-07-28 |
| **Author** | Georgiy Marinov (Infrastructure) |
| **Applies to** | `ach-model-client`, `new-customers-model-client` workers |
| **Parent** | `docs/champion-challenger-prod-plan.md` |

---

## 1. Goal

Score every transaction with **both** the champion and the challenger model, use **only** the champion's answer, and log both scores against one transaction id so the models can be compared offline.

This is the measurement phase. It must be indistinguishable from today's behaviour from the outside: same decisions, same latency, same failure modes.

---

## 2. Why not the existing switch

Today (`external-services/ach-model-api/client_predict.go:62`):

```go
endpoint := sc.endpoint
if sc.unleashClient != nil && sc.unleashClient.IsEnabled(useChallengerToggleName) {
    endpoint = sc.challengerEndpoint
}
```

Three problems:

1. **Either/or.** A transaction is scored by one model or the other. Two scores for the same transaction never exist, so the models cannot be compared on identical input.
2. **No Unleash context.** `IsEnabled()` without `unleash.WithContext(...)` cannot be sticky per customer. A Zeebe retry of the same job may be routed to a different model than the first attempt.
3. **Nothing is recorded.** Neither the arm nor the challenger's score appears in the decision or in any log we own.

`new-customers-model-api` has `challengerEndpoint` wired in the constructor (`client.go:39,48`) but never reads it, and has no Unleash client at all — the field is dead.

---

## 3. Design

```
Camunda worker
   │
   ├─► champion   (synchronous, response used)        ──► decision, as today
   │
   └─► challenger (goroutine, response DISCARDED)
            │
            └─► prediction event ──► Kafka
```

### 3.1 Hard requirements

| # | Requirement | Why |
|---|---|---|
| R1 | The challenger call **must never** block, delay or fail the champion path | It is a measurement, not a dependency |
| R2 | Separate `http.Client` with its own connection pool for the challenger | A shared pool lets a slow challenger starve the champion of connections |
| R3 | Hard timeout on the challenger, ~200 ms, independent of the champion timeout | Bounded resource use |
| R4 | Challenger errors, timeouts and panics are swallowed — metric only, no log spam, never a job failure | A broken challenger must be invisible to production |
| R5 | Bounded concurrency (worker pool or semaphore) with a drop policy on overflow | Under load, shed shadow work rather than queue it |
| R6 | Emitting the event must not block either | Non-blocking producer, drop on full buffer |
| R7 | Shadow is switchable at runtime and defaults to **off** | Kill switch without a deploy |

### 3.2 Sketch

```go
// Fire-and-forget. Never returns an error to the caller.
func (sc *ServiceClient) shadowPredict(
    parent context.Context,
    requestDto any,
    txnID string,
    championScore float64,
    logger *slog.Logger,
) {
    if sc.challengerEndpoint == "" || !sc.shadowEnabled() {
        return
    }

    select {
    case sc.shadowSem <- struct{}{}: // bounded concurrency
    default:
        metrics.ShadowDroppedCounter.WithLabelValues(sc.modelName).Inc()
        return
    }

    go func() {
        defer func() {
            <-sc.shadowSem
            if r := recover(); r != nil {
                metrics.ShadowPanicCounter.WithLabelValues(sc.modelName).Inc()
            }
        }()

        // Detached from the request context on purpose: the parent is cancelled
        // as soon as the champion responds.
        ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), sc.shadowTimeout)
        defer cancel()

        start := time.Now()
        score, err := sc.callChallenger(ctx, requestDto)
        metrics.ShadowLatency.WithLabelValues(sc.modelName).Observe(time.Since(start).Seconds())
        if err != nil {
            metrics.ShadowErrorCounter.WithLabelValues(sc.modelName, classify(err)).Inc()
            return
        }

        sc.emitPredictionEvent(txnID, championScore, score)
    }()
}
```

`context.WithoutCancel` (Go 1.21+) is important: the worker's context is cancelled the moment the champion path completes, which would kill the shadow call before it finishes.

### 3.3 Call site

```go
resp, err := sc.httpClient.Do(req)          // champion — unchanged
// … existing handling …

sc.shadowPredict(ctx, requestDto, data.ExternalID, predictionResponse.Score, logger)

return predictionResponse, err              // unchanged
```

---

## 4. Prediction event

One event per model per transaction, emitted by both the champion and the shadow path.

```jsonc
{
  "eventId":       "uuid",
  "transactionId": "CPAS_ID / externalId",
  "assignmentKey": "profileId",
  "arm":           "champion|shadow|challenger",
  "rolloutPct":    0,
  "model":         "ach|new-customers",
  "modelVersion":  "2.2.2",
  "score":         0.87,
  "decision":      "accept|challenge|deny|null",
  "featureHash":   "sha256:…",
  "latencyMs":     42,
  "ts":            "2026-07-28T10:00:00Z"
}
```

Rules:

- **Never** include the request body. It contains `CARD_NUMBER`, `CARD_CVV_AUTHORIZATION_CODE`, `SENDER_*` — PCI/PII. Ship identifiers, the score and a feature *hash* only.
- `arm = "shadow"` while the challenger has no decision authority; `"challenger"` once it does.
- `decision` is `null` for shadow events — a shadow score produces no decision.
- Both events share `transactionId`; the offline join is on that field.

Producer: non-blocking, bounded buffer, drop-and-count on overflow. Kafka availability must never affect scoring.

---

## 5. Metrics

| metric | labels | purpose |
|---|---|---|
| `shadow_scoring_latency_seconds` | `model` | challenger latency, separate from the champion |
| `shadow_scoring_errors_total` | `model`, `reason` | timeout / connection / 5xx / decode |
| `shadow_scoring_dropped_total` | `model` | shed due to concurrency limit |
| `shadow_scoring_panics_total` | `model` | must stay at zero |
| `shadow_score_delta` | `model` | histogram of `challengerScore − championScore` |

Additionally, add a `model_version` label to the existing `decisions_counter` so deny rate can be split per arm.

`shadow_score_delta` is the cheapest early signal: if the challenger is broken or mis-wired the histogram collapses to a point mass or explodes, visible within minutes and without waiting for chargebacks.

---

## 6. Configuration

```yaml
achModelClient:
  endpoint: https://ach-model-hal.mr.bossrevolution.com/...
  challengerEndpoint: https://ach-model-challenger-hal.mr.bossrevolution.com/...
  timeoutSeconds: 5
  shadow:
    enabled: false          # default off
    timeoutMs: 200
    maxConcurrent: 50
```

Runtime kill switch via the existing Unleash client, e.g. `AchModelShadowScoringEnabled`. Unleash unavailable ⇒ shadow **off** (fail-safe: the client is `nil` when `Unleash.URL` is empty, and the current code already tolerates that).

---

## 7. Later — weighted mode

The same call site extends to the weighted ramp; shadow is not thrown away.

```go
uctx := unleashcontext.Context{UserId: profileID}
useChallenger := sc.unleashClient.IsEnabled(toggleName, unleash.WithContext(uctx))

if useChallenger {
    req.Header.Set("X-Model-Arm", "challenger")   // nginx canary-by-header routes it
    arm = "challenger"
}
// arm goes into the decision record and the prediction event
```

Requires `flexibleRollout` with `stickiness = userId` on the Unleash side. Assignment becomes a normalised MurmurHash of `(groupId + userId)` → 0..100: deterministic, identical across pods, stable across restarts, and consistent across Zeebe retries.

Recommended: keep shadow running during the weighted ramp. Cost is one extra call; benefit is that comparison data stays at 100 % coverage regardless of the ramp percentage.

---

## 8. Testing

| Case | Expected |
|---|---|
| Challenger returns 500 | champion decision unchanged; `shadow_scoring_errors_total` +1 |
| Challenger hangs past the timeout | champion latency unchanged; error counter +1 |
| Challenger endpoint empty / unset | no goroutine started, no metric, no log |
| Kafka unavailable | scoring unaffected; drop counter increments |
| Shadow toggle off | no challenger call at all |
| Load test | champion p95 within noise of the pre-change baseline |

The load test is the acceptance gate: **champion p95 must not move.**

---

## 9. Estimate

| Item | Size |
|---|---|
| `shadowPredict` + wiring in two clients | ~150–200 LOC |
| Kafka producer + event schema | ~100 LOC |
| Metrics | ~40 LOC |
| Config + Helm values | small |
| Tests | ~150 LOC |

Roughly 2–3 days for one Go developer familiar with the codebase, excluding the Kafka topic provisioning (infrastructure side).

---

## 10. Open questions for the Terminus team

1. Where should the Kafka producer live — per worker, or a shared package in `terminus-core`?
2. Is `externalId` the right join key for chargebacks, or should it be `CPAS_ID`?
3. Should the shadow score also be written into `VerificationDecisionV2` (as a non-decisive `VerificationSource`), or stay only in the event stream?
4. `featureHash` — can the worker compute it, or must the model return it?
