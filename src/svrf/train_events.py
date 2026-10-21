"""Project original gate and merge events into the current owner's saved state."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

MAX_GATE_HISTORY = 128


def project_receipt(state: dict, receipt: dict, path: Path, digest: str, observed_at: float) -> None:
    history = state.setdefault("train_gate_history", {})
    families = {row["id"]: row for row in receipt.get("families", [])}
    for index, gate in enumerate(receipt.get("gates", [])):
        family = families.get(gate.get("family"), {})
        numbers = gate.get("prs") or []
        alerts = [row for row in receipt.get("alerts", [])
                  if (row.get("family") == gate.get("family")
                      or row.get("number", row.get("before")) in numbers
                      or not any(key in row for key in ("family", "number", "before")))]
        keys = ("family", "prs", "base", "commit", "tree", "green", "read_failure", "completed_event")
        history[f"{path.name}:{index}"] = deepcopy({
            "receipt": str(path), "receipt_sha256": digest, "gate_index": index,
            **{key: gate.get(key) for key in keys},
            "reused_from": gate.get("reused"), "observed_at": observed_at,
            "qualification": {"family_status": family.get("status"), "observed_at": observed_at,
                              "alerts": alerts},
            "merges": [row for row in receipt.get("merges", []) if row.get("family") == gate.get("family")],
        })
    recent = sorted(history, key=lambda key: (
        (history[key].get("completed_event") or {}).get("wall", 0),
        history[key].get("receipt", ""), history[key].get("gate_index", 0)))
    if len(recent) > MAX_GATE_HISTORY:
        state["train_gate_history_truncated"] = True
        state["train_gate_history"] = {key: history[key] for key in recent[-MAX_GATE_HISTORY:]}
