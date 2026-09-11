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

"""Bigtable MCP Toolset connecting to the Cloud Run Database Toolbox microservice.

Portability contract:
  * No shell command is executed at module import time. Credentials are resolved
    lazily, on first request, so this module imports cleanly in build, lint, CI
    and unauthenticated runtime environments.
  * No project id, project number or endpoint is hardcoded. The endpoint is
    resolved from the environment, optionally composed from the project number.
"""

import logging
import os
import subprocess
import time
from typing import Any

from dotenv import load_dotenv
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

# Load .env before any endpoint or resource name is resolved from the environment.
load_dotenv()

logger = logging.getLogger(__name__)

# Identity tokens are valid for roughly one hour; refresh well before expiry.
_TOKEN_TTL_SECONDS = 2700
_GCLOUD_TIMEOUT_SECONDS = 15

# Placeholder used when the endpoint is not configured. It is intentionally an
# unroutable .invalid host so that a misconfiguration fails loudly at call time
# instead of silently targeting someone else's project.
_UNCONFIGURED_ENDPOINT = "https://bigtable-mcp-endpoint-not-configured.invalid"

_token_cache: dict[str, Any] = {"token": None, "expiry": 0.0}


def resolve_service_url() -> str:
    """Resolves the Cloud Run MCP endpoint from the environment.

    Resolution order:
      1. ``BIGTABLE_MCP_SERVICE_URL`` - the fully qualified endpoint.
      2. Composed from ``GOOGLE_CLOUD_PROJECT_NUMBER`` plus the optional
         ``BIGTABLE_MCP_SERVICE_NAME`` and ``BIGTABLE_MCP_REGION``.

    Returns:
        The endpoint base URL without a trailing slash, or an unroutable
        placeholder when the deployment has not been configured.
    """
    explicit = os.getenv("BIGTABLE_MCP_SERVICE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    project_number = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    if project_number:
        service = os.getenv("BIGTABLE_MCP_SERVICE_NAME", "mcp-toolbox-bigtable").strip()
        region = os.getenv("BIGTABLE_MCP_REGION", "us-central1").strip()
        return f"https://{service}-{project_number}.{region}.run.app"

    logger.warning(
        "Bigtable MCP endpoint is not configured. Set BIGTABLE_MCP_SERVICE_URL, or set "
        "GOOGLE_CLOUD_PROJECT_NUMBER together with BIGTABLE_MCP_SERVICE_NAME and "
        "BIGTABLE_MCP_REGION. Real-time cashier tools will be unavailable until then."
    )
    return _UNCONFIGURED_ENDPOINT


def _fetch_token_via_adc(audience: str) -> str | None:
    """Fetches an OIDC identity token using Application Default Credentials."""
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        return id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience
        )
    except Exception as exc:  # noqa: BLE001 - ADC is best-effort, gcloud is the fallback
        logger.debug("ADC identity token unavailable: %s", exc)
        return None


def _fetch_token_via_gcloud() -> str | None:
    """Fetches an OIDC identity token via the gcloud CLI as a local dev fallback."""
    try:
        return (
            subprocess.check_output(  # noqa: S603 - fixed argv, no shell, bounded timeout
                ["gcloud", "auth", "print-identity-token"],
                stderr=subprocess.DEVNULL,
                timeout=_GCLOUD_TIMEOUT_SECONDS,
            )
            .decode()
            .strip()
        )
    except Exception as exc:  # noqa: BLE001 - absence of gcloud must never crash the app
        logger.debug("gcloud identity token unavailable: %s", exc)
        return None


def get_oidc_auth_headers(readonly_context: Any | None = None) -> dict[str, str]:
    """Returns cached OIDC auth headers for the Cloud Run MCP service.

    Called lazily by the ADK toolset on each request, never at import time.
    Returns an empty mapping when no credential can be obtained so that callers
    surface a transport error rather than an import-time crash.
    """
    now = time.time()
    cached = _token_cache.get("token")
    if cached and now < _token_cache.get("expiry", 0.0):
        return {"Authorization": f"Bearer {cached}"}

    audience = resolve_service_url()
    token = _fetch_token_via_adc(audience) or _fetch_token_via_gcloud()

    if token:
        _token_cache["token"] = token
        _token_cache["expiry"] = now + _TOKEN_TTL_SECONDS
        return {"Authorization": f"Bearer {token}"}

    if cached:
        # Serve the stale token rather than dropping auth entirely.
        logger.warning("Reusing expired identity token; refresh failed.")
        return {"Authorization": f"Bearer {cached}"}

    logger.error(
        "Unable to obtain a Google Cloud identity token for %s. Configure "
        "Application Default Credentials or authenticate with gcloud.",
        audience,
    )
    return {}


bigtable_mcp_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=f"{resolve_service_url()}/mcp",
        timeout=30.0,
    ),
    header_provider=get_oidc_auth_headers,
    tool_filter=["query_cashier_realtime_alerts", "list_tables"],
)

__all__ = ["bigtable_mcp_toolset", "get_oidc_auth_headers", "resolve_service_url"]
