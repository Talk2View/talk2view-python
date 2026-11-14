"""Where the end-user's stored model pick meets the model list.

The chat page stores a pick as a model id and, for a model that offers them, a reasoning
effort. ``resolve_pick`` is the one place that pick is read against the list the engine
gives this end-user: which row the page shows, which effort it shows, and what is sent.

An older pick may name a model id that carried its effort in the id itself
(``X-thinking``, ``X-thinking-high``, each with an optional ``-private``). Once the
engine lists ``X`` with that effort instead, such a pick is shown and sent as ``X`` at
that effort, which the engine runs exactly as it ran the old id. The stored value is
never rewritten for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["ResolvedPick", "legacy_model_effort", "resolve_pick"]

_PRIVATE = "-private"
#: Older id suffixes and the effort each one meant, longest first.
_LEGACY_SUFFIXES = (("-thinking-high", "high"), ("-thinking", "medium"))


@dataclass(frozen=True)
class ResolvedPick:
    """A stored pick, read against the model list."""

    model: str  # the listed id: shown as models.selected and sent as `model`
    effort: Optional[str]  # sent as reasoning_effort; None: send none
    shown_effort: Optional[str]  # models.selectedEffort
    drop_stored_effort: bool  # the stored effort is no longer offered for this model


def legacy_model_effort(model_id: str) -> Optional[Tuple[str, str]]:
    """``(base id, effort)`` for an id that names its effort (``X-thinking`` is
    ``(X, "medium")``, ``X-thinking-high`` is ``(X, "high")``, and a trailing
    ``-private`` stays on the base); None for any other id."""
    private = model_id.endswith(_PRIVATE)
    stem = model_id[: -len(_PRIVATE)] if private else model_id
    for suffix, effort in _LEGACY_SUFFIXES:
        if stem.endswith(suffix):
            base = stem[: -len(suffix)]
            if not base:
                return None
            return (base + _PRIVATE if private else base), effort
    return None


def _efforts(entry: Dict[str, Any]) -> Optional[List[str]]:
    efforts = entry.get("reasoning_efforts")
    return efforts if isinstance(efforts, list) else None


def resolve_pick(
    models: List[Dict[str, Any]], model_id: Optional[str], effort: Optional[str]
) -> Optional[ResolvedPick]:
    """The stored pick ``(model_id, effort)`` against ``models`` (the page's ``models.list``
    entries). None when there is no pick or its model is not listed (an orphan: the
    partner's default applies). An effort is sent only for a model that declares it; a
    model with no efforts gets none whatever is stored, and the stored one is left as it
    is. Gated efforts are resolved like any other: the engine decides whether this
    end-user may use one."""
    if model_id is None:
        return None
    by_id = {m.get("id"): m for m in models}
    implied: Optional[str] = None
    entry = by_id.get(model_id)
    if entry is None:
        legacy = legacy_model_effort(model_id)
        if legacy is None:
            return None
        base, implied = legacy
        entry = by_id.get(base)
        if entry is None:
            return None
        base_efforts = _efforts(entry)
        if base_efforts is None or implied not in base_efforts:
            return None
    listed_id: str = entry["id"]
    efforts = _efforts(entry)
    if efforts is None:
        return ResolvedPick(listed_id, None, None, False)
    wanted = effort if effort is not None else implied
    default = entry.get("default_reasoning_effort")
    if wanted is None:
        return ResolvedPick(listed_id, None, default, False)
    if wanted in efforts:
        return ResolvedPick(listed_id, wanted, wanted, False)
    return ResolvedPick(listed_id, None, default, effort is not None)
