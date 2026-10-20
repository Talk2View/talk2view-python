"""Replay contract/ops/cases.json through ``state._apply_op``.

The embed page's applier (``embed/src/ops.ts``) replays the same cases
(``embed/tests/ops.test.ts``), so a batch that applies here applies there, and
one that fails here fails there.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from talk2view.state import OpError, _apply_op

CASES_FILE = Path(__file__).resolve().parents[1] / "contract" / "ops" / "cases.json"
CASES: List[Dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def test_every_case_has_one_outcome() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert ("expect" in case) != bool(case.get("error")), case["id"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_python_applies_the_case(case: Dict[str, Any]) -> None:
    root = copy.deepcopy(case["state"])
    if case.get("error"):
        with pytest.raises(OpError):
            for op in case["ops"]:
                _apply_op(root, op)
        return
    for op in case["ops"]:
        _apply_op(root, op)
    assert root == case["expect"]
