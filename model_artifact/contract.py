"""
Build, validate, read, and write ``model_meta.json`` — the model artifact
contract described in ``docs/model-artifact-contract.md``.

Validation uses ``jsonschema`` against ``schemas/model_meta.schema.json``
when that package is installed (not a project dependency today); otherwise
it falls back to a lighter structural check covering the same required
fields. Either way, ``write_model_meta``/``read_model_meta`` never
silently accept a malformed dict — they raise ``ModelMetaError`` listing
every violation found.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
MODEL_FAMILIES = ("catboost", "xgboost", "random_forest")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "model_meta.schema.json"

_REQUIRED_TOP_LEVEL = (
    "schema_version",
    "clearml_task_id",
    "task_name",
    "project_name",
    "model_family",
    "created_at",
    "metrics",
    "threshold",
    "features",
    "artifacts",
    "data",
)


class ModelMetaError(ValueError):
    """Raised when a model_meta.json dict fails contract validation."""


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def build_model_meta(
    *,
    task: Any,
    task_name: str,
    project_name: str,
    model_family: str,
    metrics: dict,
    threshold: dict,
    features: dict,
    artifacts: dict,
    data: dict,
    model_version: str | None = None,
    git_revision: str | None = None,
    notes: str | None = None,
    created_at: str | None = None,
) -> dict:
    """
    Assemble a ``model_meta.json``-shaped dict.

    ``task`` is a ClearML ``Task`` or the local ``NullTask`` stand-in
    (``fraud_pipeline.clearml_backend.NullTask``) — only ``task.id`` is
    read from it, since ``NullTask`` has no name/project attributes.
    ``task_name``/``project_name`` are passed explicitly because trainers
    already have them (they're what was given to ``Task.init``).

    Raises ``ModelMetaError`` if the assembled dict doesn't satisfy the
    contract — see ``validate_model_meta`` for the exact rules.
    """
    meta = {
        "schema_version": SCHEMA_VERSION,
        "clearml_task_id": str(getattr(task, "id", "local")),
        "task_name": task_name,
        "project_name": project_name,
        "model_family": model_family,
        "model_version": model_version,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "git_revision": git_revision,
        "notes": notes,
        "metrics": metrics,
        "threshold": threshold,
        "features": features,
        "artifacts": artifacts,
        "data": data,
    }

    errors = validate_model_meta(meta)
    if errors:
        raise ModelMetaError(
            "Refusing to build an invalid model_meta.json:\n  - " + "\n  - ".join(errors)
        )
    return meta


def validate_model_meta(meta: dict) -> list[str]:
    """
    Return a list of contract violations (empty means valid).

    Uses ``jsonschema`` against ``schemas/model_meta.schema.json`` when
    available for full validation; otherwise checks the required fields
    by hand (less exhaustive, but catches the common mistakes: missing
    sections, empty feature lists, an unknown model family).
    """
    try:
        import jsonschema
    except ImportError:
        return _validate_by_hand(meta)

    validator_cls = jsonschema.Draft202012Validator
    validator = validator_cls(_load_schema())
    return [
        f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(meta), key=lambda e: list(e.path))
    ]


def _validate_by_hand(meta: dict) -> list[str]:
    errors: list[str] = []

    if not isinstance(meta, dict):
        return ["model_meta must be a JSON object"]

    for key in _REQUIRED_TOP_LEVEL:
        if key not in meta:
            errors.append(f"missing required field: {key}")

    if meta.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")

    if meta.get("model_family") not in MODEL_FAMILIES:
        errors.append(f"model_family must be one of {MODEL_FAMILIES}")

    metrics = meta.get("metrics") or {}
    for key in ("roc_auc", "pr_auc"):
        if key not in metrics:
            errors.append(f"metrics.{key} is required")

    threshold = meta.get("threshold") or {}
    for key in ("value", "method"):
        if key not in threshold:
            errors.append(f"threshold.{key} is required")

    features = meta.get("features") or {}
    names = features.get("names")
    if not names:
        errors.append("features.names must be a non-empty list")
    if not features.get("count"):
        errors.append("features.count is required")

    artifacts = meta.get("artifacts") or {}
    if not artifacts:
        errors.append("artifacts must have at least one entry")
    for name, entry in artifacts.items():
        if not isinstance(entry, dict) or not entry.get("path"):
            errors.append(f"artifacts.{name}.path is required")

    data = meta.get("data") or {}
    for key in ("train_path", "test_path"):
        if not data.get(key):
            errors.append(f"data.{key} is required")

    return errors


def write_model_meta(path: str | Path, meta: dict) -> None:
    """Validate ``meta`` and write it to ``path`` as indented JSON."""
    errors = validate_model_meta(meta)
    if errors:
        raise ModelMetaError(
            f"Refusing to write invalid model_meta.json to {path}:\n  - "
            + "\n  - ".join(errors)
        )
    Path(path).write_text(json.dumps(meta, indent=2, sort_keys=False) + "\n")


def read_model_meta(path: str | Path) -> dict:
    """Read and validate a ``model_meta.json`` file. Raises ``ModelMetaError`` if invalid."""
    meta = json.loads(Path(path).read_text())
    errors = validate_model_meta(meta)
    if errors:
        raise ModelMetaError(
            f"{path} does not satisfy the model artifact contract:\n  - "
            + "\n  - ".join(errors)
        )
    return meta
