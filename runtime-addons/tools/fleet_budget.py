"""fleet_budget — the ONE implementation of the cross-bridge delegation budget.

Three copies used to exist: the bridge worker (hermes-bridge/worker.py,
``max_fleet_depth`` default 3), ``tools/fleet_send.py`` (propagates the next-hop
fields) and ``tools/delegate_neith.py`` (refuses over budget — with its own
default of 1, R2-2-23: any agent handling a bus-delivered task, depth 1, was
refused research delegation while every other hop on the fleet allowed it).
The runtime's two tools now share this module; the bridge worker's constant is
pinned by ``MAX_FLEET_DEPTH_DEFAULT`` and its contract test.

Contract with the bridge worker (unchanged): it exports FLEET_DELEGATION_DEPTH
/ _ORIGIN / _VISITED into the worker env when the process runs a delegated
task, and reads ``delegation_depth`` / ``delegation_origin`` /
``delegation_visited`` off every inbound task. ``MAX_FLEET_DEPTH`` overrides the
cap everywhere; a bad value falls back to the default.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

#: Same default as hermes-bridge/worker.py::max_fleet_depth (raised 1 -> 3 on
#: 2026-08-13 so thoth -> neith -> ptah collaboration works; the visited-chain
#: loop check is the real guard).
MAX_FLEET_DEPTH_DEFAULT = 3
FLEET_VISITED_MAX = 16


def max_fleet_depth() -> int:
    """Cross-bridge delegation hop cap (env MAX_FLEET_DEPTH, default 3)."""
    try:
        return max(0, int(os.environ.get("MAX_FLEET_DEPTH", str(MAX_FLEET_DEPTH_DEFAULT))))
    except ValueError:
        return MAX_FLEET_DEPTH_DEFAULT


def normalize_visited(raw) -> List[str]:
    """Lower-cased, de-duplicated, bounded chain from a list or a comma-separated string."""
    if isinstance(raw, str):
        raw = raw.split(",")
    visited: List[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        name = str(item).strip().lower()
        if name and name not in visited:
            visited.append(name)
        if len(visited) >= FLEET_VISITED_MAX:
            break
    return visited


def budget_from_env() -> Tuple[int, str, List[str]]:
    """``(depth, origin, visited)`` seeded by the bridge worker, if any."""
    try:
        depth = max(0, int(os.environ.get("FLEET_DELEGATION_DEPTH", "0")))
    except ValueError:
        depth = 0
    origin = os.environ.get("FLEET_DELEGATION_ORIGIN", "").strip().lower()
    return depth, origin, normalize_visited(os.environ.get("FLEET_DELEGATION_VISITED", ""))


def next_hop_fields(sender: str) -> dict:
    """The budget to attach to an outbound hop from ``sender``: depth + 1, the
    origin (or the sender when this is the first hop), the chain with the sender
    appended once."""
    depth, origin, visited = budget_from_env()
    sender_l = (sender or "").strip().lower()
    if sender_l and sender_l not in visited:
        visited.append(sender_l)
    return {
        "delegation_depth": depth + 1,
        "delegation_origin": origin or sender_l,
        "delegation_visited": visited,
    }


def refusal_reason(target: str, sender: str, depth: int, visited) -> Optional[str]:
    """Why a hop from ``sender`` to ``target`` must not leave the box, or None."""
    sender_l, target_l = (sender or "").strip().lower(), (target or "").strip().lower()
    if sender_l and sender_l == target_l:
        return f"you ARE {target} — delegating to {target} would send the task to yourself"
    cap = max_fleet_depth()
    if depth >= cap:
        return (f"this conversation was itself delegated across {depth} fleet "
                f"hop(s), which exhausts the limit of {cap}")
    chain = normalize_visited(visited)
    if target_l in chain:
        return (f"{target} already handled this request "
                f"(chain: {' -> '.join(chain)}), so delegating back would loop")
    return None
