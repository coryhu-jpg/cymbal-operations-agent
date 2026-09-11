# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hermetic validation of the evaluation assets.

Guards the eval suite against silent drift: schema integrity, BRD traceability,
golden-response presence, and agreement between the agent's guardrail strings
and the golden datasets.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "tests" / "eval"
DATASETS = EVAL_DIR / "datasets"
CONTRACTS = EVAL_DIR / "contracts"

GOLDEN = DATASETS / "golden-data.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Structural integrity
# --------------------------------------------------------------------------


def test_all_eval_assets_are_parseable() -> None:
    yaml = pytest.importorskip("yaml")

    for path in sorted(DATASETS.glob("*.json")) + sorted(CONTRACTS.glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(CONTRACTS.glob("*.yaml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8"))
    assert yaml.safe_load((EVAL_DIR / "eval_config.yaml").read_text(encoding="utf-8"))


def test_golden_cases_follow_adk_schema() -> None:
    """Every golden case needs an id, a BRD tag and a well-formed conversation."""
    golden = _load(GOLDEN)
    assert golden["eval_cases"], "golden dataset is empty"

    seen: set[str] = set()
    for case in golden["eval_cases"]:
        eval_id = case["eval_id"]
        assert eval_id not in seen, f"duplicate eval_id: {eval_id}"
        seen.add(eval_id)

        assert case["brd_requirements"], f"{eval_id} has no BRD requirement tag"
        assert case["session_input"]["app_name"]
        assert case["conversation"], f"{eval_id} has no conversation turns"

        for turn in case["conversation"]:
            assert turn["user_content"]["parts"][0]["text"].strip()
            assert turn["final_response"]["parts"][0]["text"].strip(), (
                f"{eval_id}/{turn['invocation_id']} is missing a golden final_response, "
                "which makes response_match_score uncomputable"
            )
            assert isinstance(turn["intermediate_data"]["tool_uses"], list)


def test_zero_tool_turns_declare_a_rationale() -> None:
    """An empty expected trajectory must be deliberate, not accidental."""
    for case in _load(GOLDEN)["eval_cases"]:
        for turn in case["conversation"]:
            assertions = turn.get("assertions") or {}
            if assertions.get("expected_tool_use_count") != 0:
                continue
            assert turn["intermediate_data"]["tool_uses"] == []
            assert assertions.get("rationale_for_zero_tool_use"), (
                f"{case['eval_id']}/{turn['invocation_id']} expects zero tools but "
                "does not explain why"
            )


def test_declared_tool_use_count_matches_trajectory() -> None:
    for case in _load(GOLDEN)["eval_cases"]:
        for turn in case["conversation"]:
            assertions = turn.get("assertions") or {}
            expected = assertions.get("expected_tool_use_count")
            if expected is None:
                continue
            actual = len(turn["intermediate_data"]["tool_uses"])
            assert actual == expected, (
                f"{case['eval_id']}/{turn['invocation_id']}: declared "
                f"{expected} tool uses but trajectory has {actual}"
            )


def test_only_real_tools_are_referenced() -> None:
    known = {
        "pos_troubleshooting_rag_tool",
        "cymbal_analytics_tool",
        "query_cashier_realtime_alerts",
        "list_tables",
    }
    for case in _load(GOLDEN)["eval_cases"]:
        for turn in case["conversation"]:
            for use in turn["intermediate_data"]["tool_uses"]:
                assert use["name"] in known, f"unknown tool: {use['name']}"


# --------------------------------------------------------------------------
# Coverage guarantees
# --------------------------------------------------------------------------


def test_brd_traceability_matrix_points_at_real_cases() -> None:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((EVAL_DIR / "eval_config.yaml").read_text(encoding="utf-8"))
    ids = {case["eval_id"] for case in _load(GOLDEN)["eval_cases"]}

    for requirement, entry in config["brd_traceability_matrix"].items():
        assert entry["covered_by"], f"{requirement} lists no cases"
        for case_id in entry["covered_by"]:
            assert case_id in ids, f"{requirement} references unknown case {case_id}"


@pytest.mark.parametrize(
    "requirement", ["FR-1.1", "FR-1.5", "FR-5.1", "NFR-1.1", "NFR-1.2", "NFR-3.3", "NFR-4.1"]
)
def test_critical_requirements_are_covered(requirement: str) -> None:
    covered = {
        req
        for case in _load(GOLDEN)["eval_cases"]
        for req in case["brd_requirements"]
    }
    assert requirement in covered, f"{requirement} has no eval coverage"


def test_multi_turn_guardrail_coverage_exists() -> None:
    """MULTI_TURN_GUARDRAILS: PII masking and the partition clarification pause."""
    cases = _load(GOLDEN)["eval_cases"]
    multi_turn = [c for c in cases if len(c["conversation"]) > 1]
    assert len(multi_turn) >= 3

    blob = json.dumps(cases)
    assert "XXXX-XXXX-XXXX-9999" in blob, "no payment card masking coverage"
    assert "CLARIFICATION REQUIRED" in blob, "no date-range clarification coverage"

    # The masking guardrail must be re-attacked on a later turn, not just turn 1.
    persisted = [
        c
        for c in multi_turn
        if any(
            (t.get("assertions") or {}).get("guardrail_persists_across_turns")
            or (t.get("assertions") or {}).get("guardrail_persists_after_successful_turn")
            for t in c["conversation"]
        )
    ]
    assert persisted, "no case verifies a guardrail surviving a later turn"


# --------------------------------------------------------------------------
# Implementation / dataset agreement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "SECURITY BLOCK",
        "POLICY BLOCK",
        "PII BLOCK",
        "CLARIFICATION REQUIRED",
        "XXXX-XXXX-XXXX-9999",
        "Regional Store data is currently unreachable",
    ],
)
def test_agent_prompt_defines_every_asserted_guardrail(phrase: str) -> None:
    """A dataset assertion is only valid if the agent is instructed to satisfy it."""
    from app.agent import CYMBAL_OPERATIONS_INSTRUCTIONS

    assert phrase in CYMBAL_OPERATIONS_INSTRUCTIONS, (
        f"golden datasets assert '{phrase}' but the agent instruction never defines it"
    )


def test_agent_binds_all_three_toolsets() -> None:
    from app.agent import cymbal_operations_agent

    assert cymbal_operations_agent.name == "cymbal_operations_agent"
    assert len(cymbal_operations_agent.tools) == 3
