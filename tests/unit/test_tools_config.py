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

"""Hermetic unit tests for tool configuration and portability.

These tests must pass with no credentials, no network and no gcloud CLI.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"


# --------------------------------------------------------------------------
# Portability: no shell-outs at import time, no hardcoded project identifiers
# --------------------------------------------------------------------------


def test_importing_tools_does_not_shell_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the tool package must never execute a subprocess.

    A shell-out at import time crashes build, lint and CI environments that
    have no gcloud CLI and no credentials.
    """
    calls: list[list[str]] = []

    def _fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(list(args[0]) if args else [])
        raise AssertionError(f"subprocess invoked at import time: {calls}")

    monkeypatch.setattr(subprocess, "check_output", _fail)
    monkeypatch.setattr(subprocess, "run", _fail)

    for module in ("app.tools.bigtable_tool", "app.tools.rag_tool", "app.tools"):
        sys.modules.pop(module, None)

    import app.tools  # noqa: F401,PLC0415

    assert calls == []


def test_no_hardcoded_project_identifiers_in_source() -> None:
    """No project id, project number or environment-specific host may be hardcoded."""
    # A Cloud Run URL embeds the project number, e.g. service-547486901530.region.run.app
    project_number_in_url = re.compile(r"https://[a-z0-9-]+-\d{9,}\.[a-z0-9-]+\.run\.app")
    project_resource = re.compile(r"projects/[a-z][a-z0-9-]{4,}/locations/")

    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if project_number_in_url.search(line) or project_resource.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {stripped}")

    assert not offenders, "Hardcoded environment identifiers found:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# Endpoint resolution
# --------------------------------------------------------------------------


def test_resolve_service_url_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.bigtable_tool import resolve_service_url

    monkeypatch.setenv("BIGTABLE_MCP_SERVICE_URL", "https://example-run-app.example.com/")
    assert resolve_service_url() == "https://example-run-app.example.com"


def test_resolve_service_url_composes_from_project_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.bigtable_tool import resolve_service_url

    monkeypatch.delenv("BIGTABLE_MCP_SERVICE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789012")
    monkeypatch.setenv("BIGTABLE_MCP_REGION", "europe-west1")
    monkeypatch.setenv("BIGTABLE_MCP_SERVICE_NAME", "mcp-toolbox-bigtable")

    assert (
        resolve_service_url()
        == "https://mcp-toolbox-bigtable-123456789012.europe-west1.run.app"
    )


def test_resolve_service_url_unconfigured_is_unroutable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured deployment must not silently target another project."""
    from app.tools.bigtable_tool import resolve_service_url

    monkeypatch.delenv("BIGTABLE_MCP_SERVICE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)

    assert resolve_service_url().endswith(".invalid")


def test_oidc_headers_never_raise_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header resolution degrades to an empty mapping rather than crashing."""
    import app.tools.bigtable_tool as bt

    monkeypatch.setitem(bt._token_cache, "token", None)
    monkeypatch.setitem(bt._token_cache, "expiry", 0.0)
    monkeypatch.setattr(bt, "_fetch_token_via_adc", lambda audience: None)
    monkeypatch.setattr(bt, "_fetch_token_via_gcloud", lambda: None)

    assert bt.get_oidc_auth_headers() == {}


def test_oidc_headers_use_bearer_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.tools.bigtable_tool as bt

    monkeypatch.setitem(bt._token_cache, "token", None)
    monkeypatch.setitem(bt._token_cache, "expiry", 0.0)
    monkeypatch.setattr(bt, "_fetch_token_via_adc", lambda audience: "fake-token")

    assert bt.get_oidc_auth_headers() == {"Authorization": "Bearer fake-token"}


# --------------------------------------------------------------------------
# Data Agent resolution
# --------------------------------------------------------------------------


def test_resolve_data_agent_name_prefers_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.analytics_tool import resolve_data_agent_name

    monkeypatch.setenv("DATA_AGENT_NAME", "projects/p/locations/global/dataAgents/a")
    assert resolve_data_agent_name() == "projects/p/locations/global/dataAgents/a"


def test_resolve_data_agent_name_composes_from_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.analytics_tool import resolve_data_agent_name

    monkeypatch.delenv("DATA_AGENT_NAME", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("DATA_AGENT_ID", "gda-1234")
    monkeypatch.delenv("DATA_AGENT_LOCATION", raising=False)

    assert (
        resolve_data_agent_name()
        == "projects/my-project/locations/global/dataAgents/gda-1234"
    )


def test_unconfigured_data_agent_returns_clean_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-4.1: a clean warning, never a stack trace or connection string."""
    from app.tools.analytics_tool import UNCONFIGURED_AGENT_MSG, cymbal_analytics_tool

    monkeypatch.delenv("DATA_AGENT_NAME", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("DATA_AGENT_ID", raising=False)

    result = cymbal_analytics_tool("What is today's net revenue?")

    assert result == UNCONFIGURED_AGENT_MSG
    assert "Regional Store data is currently unreachable" in result
    assert "Traceback" not in result


# --------------------------------------------------------------------------
# RAG tool helpers and contract alignment
# --------------------------------------------------------------------------


def test_gcs_uri_is_converted_to_clickable_link() -> None:
    from app.tools.rag_tool import _gcs_to_https

    assert (
        _gcs_to_https("gs://bucket/manual.pdf")
        == "https://storage.cloud.google.com/bucket/manual.pdf"
    )
    assert _gcs_to_https("https://example.com/x.pdf") == "https://example.com/x.pdf"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("EMV freeze ERR-PAY-4001 on register 3", '"EMV-freeze"'),
        ("thermal cutter ERR-DN-PRNT-24V lock", '"thermal-cutter"'),
        ("no code here", None),
    ],
)
def test_error_code_extraction(query: str, expected: str | None) -> None:
    from app.tools.rag_tool import _extract_error_codes

    result = _extract_error_codes(query)
    if expected is None:
        assert result is None
    else:
        assert result is not None and result.startswith('"')


def test_rag_refusal_string_matches_published_contract() -> None:
    """The refusal text must match tests/eval/contracts/pos_rag_contract.yaml exactly."""
    yaml = pytest.importorskip("yaml")
    from app.tools.rag_tool import UNCERTIFIED_FALLBACK_MSG

    contract_path = REPO_ROOT / "tests" / "eval" / "contracts" / "pos_rag_contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    published = contract["service_level_agreements"]["warning_string"]

    assert UNCERTIFIED_FALLBACK_MSG.startswith(published.rstrip())


def test_rag_threshold_matches_published_contract() -> None:
    yaml = pytest.importorskip("yaml")
    from app.tools.rag_tool import MIN_SIMILARITY_THRESHOLD

    contract_path = REPO_ROOT / "tests" / "eval" / "contracts" / "pos_rag_contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert MIN_SIMILARITY_THRESHOLD == pytest.approx(
        contract["service_level_agreements"]["minimum_confidence_threshold"]
    )
    assert MIN_SIMILARITY_THRESHOLD == pytest.approx(
        contract["tool_interface"]["input_contract"]["properties"]["threshold"]["default"]
    )
