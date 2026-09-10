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

"""Bigtable MCP Toolset connecting to Cloud Run Database Toolbox microservice."""

import os
import subprocess
import time
from typing import Any

from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

_token_cache: dict[str, Any] = {"token": None, "expiry": 0}


def get_oidc_auth_headers(readonly_context: Any | None = None) -> dict[str, str]:
    """Generates and caches OIDC identity token for Cloud Run MCP authentication."""
    now = time.time()
    if not _token_cache["token"] or now >= _token_cache["expiry"]:
        try:
            token = (
                subprocess.check_output(
                    ["gcloud", "auth", "print-identity-token"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            _token_cache["token"] = token
            # Cache for 45 minutes (identity tokens are typically valid for 1 hour)
            _token_cache["expiry"] = now + 2700
        except Exception as e:
            if _token_cache["token"]:
                return {"Authorization": f"Bearer {_token_cache['token']}"}
            raise RuntimeError(f"Failed to obtain GCP identity token: {e}") from e

    return {"Authorization": f"Bearer {_token_cache['token']}"}


_SERVICE_URL = os.getenv(
    "BIGTABLE_MCP_SERVICE_URL",
    "https://mcp-toolbox-bigtable-547486901530.us-central1.run.app",
).rstrip("/")
_MCP_URL = f"{_SERVICE_URL}/mcp"

_params = StreamableHTTPConnectionParams(
    url=_MCP_URL,
    headers=get_oidc_auth_headers(),
    timeout=30.0,
)

bigtable_mcp_toolset = McpToolset(
    connection_params=_params,
    header_provider=get_oidc_auth_headers,
    tool_filter=["query_cashier_realtime_alerts", "list_tables"],
)

__all__ = ["bigtable_mcp_toolset"]
