# Model artifact contract

This document defines `model_meta.json` — a single, canonical metadata sidecar
for a trained model artifact bundle (one CatBoost/XGBoost/RandomForest model
plus its test-set metrics, tuned threshold, decision-band cuts, and feature
list). It exists to stop the same information from drifting across
incompatible, hand-rolled shapes.

**Wiki index:** [index.md](index.md)

**JSON Schema:** [`schemas/model_meta.schema.json`](../schemas/model_meta.schema.json)
**Python helpers:** [`fraud_pipeline/artifacts/contract.py`](../fraud_pipeline/artifacts/contract.py)

---

## Why this exists

Today, "what does this trained model look like" is answered three different,
overlapping ways depending on where you look:

- `evaluate_and_log()` (`fraud_pipeline/evaluation/evaluator.py`) returns a
  live Python dict with `threshold`, `best_threshold`, `reference_threshold`.
- Inference packages (`packages/*/models/model_meta.json`) persist
  `threshold`, `operating_threshold`, `accept_max`, `deny_min` at the top
  level.
- The champion selector (`scripts/select_best_imtu_model.py`) writes
  `models/<dataset>/<version>/model_card.json` with its own `artifacts` dict
  shape and no `threshold_method`.

None of these three is wrong on its own, but a tool that wants to consume
"the model that's currently deployed" has to know all three shapes. This
contract is a fourth file, `model_meta.json`, that any producer (a trainer
run, `scripts/select_best_imtu_model.py`, a manual export) can write and any
consumer (champion/challenger comparison, an inference package loader, a
future model registry) can read without knowing which trainer produced it.

It does **not** replace `evaluate_and_log()`'s return dict, ClearML task
artifacts, or the existing `model_meta.json`/`model_card.json` files — those
keep working as-is. Treat `model_meta.json` as the thing you write *in
addition*, once a model is picked to ship, export, or hand off.

## Where it lives

One `model_meta.json` sits next to the model file(s) it describes:

```
models/<dataset>/<version>/
  catboost_model.cbm
  catboost_model.pkl
  model_meta.json          # <- this contract
```

or, for a ClearML task that hasn't been exported to a directory yet, it can
be uploaded as a ClearML artifact named `model_meta` (`task.upload_artifact
(name="model_meta", artifact_object=meta_dict)`) alongside the existing
`catboost_model_pickle` / `catboost_model_cbm` / `xgboost_model_json` /
`xgboost_model_pickle` / `random_forest_model_pickle` artifacts.

## Fields

See [`schemas/model_meta.schema.json`](../schemas/model_meta.schema.json) for
the enforceable version. Summary:

| Field | Required | Notes |
|---|---|---|
| `schema_version` | yes | Always `"1.0"` for this version of the contract. |
| `clearml_task_id` | yes | `"local"` for `--no-clearml` runs (matches `NullTask.id`). |
| `task_name`, `project_name` | yes | As passed to `Task.init(...)`. |
| `model_family` | yes | `catboost` \| `xgboost` \| `random_forest` — the base family that owns the leaf model file, even for stacked/hybrid variants. |
| `model_version` | no | Free-form tag, e.g. from `scripts/select_best_imtu_model.py --version`. |
| `created_at` | yes | ISO 8601 UTC. |
| `git_revision` | no | Full commit SHA of the pipeline code, when known. |
| `metrics.roc_auc`, `metrics.pr_auc` | yes | The rest of `metrics.*` (accuracy, f1, precision, recall, logloss, confusion) are optional/nullable. |
| `threshold.value` | yes | The single canonical operating threshold — same number as `decision_band_info.deny_min` when bands are enabled. Replaces the `threshold` / `operating_threshold` / `best_threshold` naming spread across existing files. |
| `threshold.method` | yes | e.g. `business_fpr_cap`, `fp_clv`, `f1`. |
| `threshold.accept_max`, `threshold.deny_min`, `threshold.bands`, `threshold.challenge_max_txns`, `threshold.challenge_max_customers` | no | Same shape as `fraud_pipeline/metrics/decision_bands.py:decision_band_info()`, carried over when decision bands are enabled. |
| `features.names`, `features.count` | yes | The exact feature list and count the model was fit on. |
| `artifacts` | yes | Maps a stable name (e.g. `catboost_model_cbm`) to `{"path": ..., "clearml_name": ...}`. `path` is relative to the `model_meta.json` file; `clearml_name` is the ClearML artifact name when the model also lives on a task. |
| `data.train_path`, `data.test_path` | yes | `data.valid_path`, `data.dataset_name` optional. |

## Naming reconciliation

When migrating an existing `model_meta.json` (inference packages) or
`model_card.json` (champion selector) into this contract:

| Legacy field | Contract field |
|---|---|
| `threshold` / `operating_threshold` | `threshold.value` |
| `accept_max` (top level) | `threshold.accept_max` |
| `deny_min` (top level) | `threshold.deny_min` |
| `bands` (top level) | `threshold.bands` |
| `feature_names` | `features.names` |
| `count` (feature count) | `features.count` |
| `model_card.json`'s `artifacts.<name>` (bare path string) | `artifacts.<name>.path` |
| `model_card.json`'s `model_version` | `model_version` (unchanged) |

## Producing one

```python
from fraud_pipeline.artifacts.contract import build_model_meta, write_model_meta

meta = build_model_meta(
    task=task,                       # ClearML Task or NullTask
    model_family="catboost",
    metrics={"roc_auc": 0.956, "pr_auc": 0.823, "f1": 0.61},
    threshold={"value": 0.510, "method": "business_fpr_cap",
               "accept_max": 0.310, "deny_min": 0.510},
    features={"names": feature_list, "count": len(feature_list)},
    artifacts={
        "catboost_model_cbm": {"path": "catboost_model.cbm", "clearml_name": "catboost_model_cbm"},
        "catboost_model_pickle": {"path": "catboost_model.pkl", "clearml_name": "catboost_model_pickle"},
    },
    data={"train_path": train_path, "valid_path": valid_path, "test_path": test_path},
)
write_model_meta("models/mt_new_customers/v1/model_meta.json", meta)
```

## Consuming one

```python
from fraud_pipeline.artifacts.contract import read_model_meta

meta = read_model_meta("models/mt_new_customers/v1/model_meta.json")
threshold = meta["threshold"]["value"]
model_path = meta["artifacts"]["catboost_model_cbm"]["path"]
```

`read_model_meta` and `write_model_meta` both validate against the contract
(`validate_model_meta`) and raise `ModelMetaError` listing every violation on
failure — they don't silently write or trust malformed metadata.

## Non-goals

- This is not a general-purpose experiment-tracking format — it describes one
  shippable model, not a training run's full history (use ClearML's own task
  page / connected params for that).
- It doesn't replace `decision_band_info` / `business_metrics_summary_table`
  as ClearML artifacts — `fraud_pipeline/clearml_backend.py`'s
  `resolve_tuned_threshold` / `resolve_bands_threshold` keep reading those
  directly off a live task. `model_meta.json` is for once a model has been
  *selected*, not for every training run.
