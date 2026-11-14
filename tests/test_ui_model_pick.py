"""talk2view.ui.model_pick: a stored model pick read against the model list."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from talk2view.ui.model_pick import ResolvedPick, legacy_model_effort, resolve_pick

G4H = {"id": "g4h", "reasoning_efforts": ["low", "medium", "high"],
       "default_reasoning_effort": "low", "gated_reasoning_efforts": ["high"]}
G4HP = {**G4H, "id": "g4h-private", "gated_reasoning_efforts": []}
PLAIN = {"id": "gpt", "reasoning_efforts": None, "default_reasoning_effort": None,
         "gated_reasoning_efforts": None}
OLD = {"id": "g4h-thinking"}  # an old engine's entry: no keys at all
MODELS = [G4H, G4HP, PLAIN]


@pytest.mark.parametrize(("model_id", "expected"), [
    ("g4h-thinking", ("g4h", "medium")),
    ("g4h-thinking-high", ("g4h", "high")),
    ("g4h-thinking-private", ("g4h-private", "medium")),
    ("g4h-thinking-high-private", ("g4h-private", "high")),
    ("g4h", None),
    ("g4h-private", None),
    ("-thinking", None),
    ("-thinking-high-private", None),
    ("thinking-high-fan", None),
])
def test_legacy_model_effort(model_id: str, expected: Optional[Tuple[str, str]]) -> None:
    assert legacy_model_effort(model_id) == expected


@pytest.mark.parametrize(("models", "model_id", "effort", "expected"), [
    (MODELS, None, None, None),
    (MODELS, "gone", None, None),
    (MODELS, "gpt", None, ResolvedPick("gpt", None, None, False)),
    (MODELS, "gpt", "high", ResolvedPick("gpt", None, None, False)),  # never sent for null efforts
    (MODELS, "g4h", None, ResolvedPick("g4h", None, "low", False)),
    (MODELS, "g4h", "medium", ResolvedPick("g4h", "medium", "medium", False)),
    (MODELS, "g4h", "high", ResolvedPick("g4h", "high", "high", False)),  # gated: sent all the same
    (MODELS, "g4h", "xhigh", ResolvedPick("g4h", None, "low", True)),  # not offered
    (MODELS, "g4h-thinking", None, ResolvedPick("g4h", "medium", "medium", False)),
    # implied and gated: sent
    (MODELS, "g4h-thinking-high", None, ResolvedPick("g4h", "high", "high", False)),
    (MODELS, "g4h-thinking-high-private", None,
     ResolvedPick("g4h-private", "high", "high", False)),
    (MODELS, "g4h-thinking", "low", ResolvedPick("g4h", "low", "low", False)),  # explicit wins
    # still listed: unchanged
    ([G4H, OLD], "g4h-thinking", None, ResolvedPick("g4h-thinking", None, None, False)),
    ([PLAIN], "g4h-thinking", None, None),  # base not listed
    ([{**G4H, "reasoning_efforts": ["low"]}], "g4h-thinking", None, None),  # effort not allowed
])
def test_resolve_pick(
    models: List[Dict[str, Any]],
    model_id: Optional[str],
    effort: Optional[str],
    expected: Optional[ResolvedPick],
) -> None:
    assert resolve_pick(models, model_id, effort) == expected
