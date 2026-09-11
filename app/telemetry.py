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

"""Runtime telemetry wiring for the Cymbal Operations Agent.

Streams structured agent events (prompts, LLM responses, tool arguments,
latency, token usage, errors) to BigQuery through the ADK
``BigQueryAgentAnalyticsPlugin``, which writes asynchronously over the
BigQuery Storage Write API (gRPC) and therefore never blocks agent execution.

Portability contract (same discipline as ``app/tools/*``):
  * No project id, dataset id or region is hardcoded; everything is resolved
    from the environment.
  * Importing this module never raises. If the optional analytics extra is not
    installed, or the environment is not configured, telemetry degrades to a
    no-op and the agent still starts. Observability must never be able to take
    the agent down.
"""

import logging
import os

from dotenv import load_dotenv

# Load .env before any endpoint or resource name is resolved from the environment.
load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_DATASET = "agent_telemetry"
DEFAULT_TELEMETRY_TABLE = "events"
DEFAULT_TELEMETRY_LOCATION = "us-central1"


def resolve_telemetry_project() -> str:
    """Resolves the project that owns the telemetry dataset.

    ``PROJECT_ID`` is the name used by the lab guide; ``GOOGLE_CLOUD_PROJECT``
    is the canonical name used everywhere else in this repository. Both are
    honoured so the same code runs under either convention.
    """
    for var in ("PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return ""


def resolve_telemetry_dataset() -> str:
    """Resolves the BigQuery dataset that receives agent telemetry events."""
    for var in ("BQ_TELEMETRY_DATASET", "BQ_ANALYTICS_DATASET_ID"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return DEFAULT_TELEMETRY_DATASET


def resolve_telemetry_location() -> str:
    """Resolves the BigQuery location of the telemetry dataset.

    This must match the dataset's actual location or the Storage Write API
    stream will be rejected.
    """
    for var in ("REGION", "BQ_TELEMETRY_LOCATION", "BIGQUERY_LOCATION"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return DEFAULT_TELEMETRY_LOCATION


def resolve_telemetry_table() -> str:
    """Resolves the telemetry event table name."""
    return os.getenv("BQ_TELEMETRY_TABLE", DEFAULT_TELEMETRY_TABLE).strip() or (
        DEFAULT_TELEMETRY_TABLE
    )


def telemetry_enabled() -> bool:
    """Returns whether BigQuery agent analytics should be wired up.

    Disabled explicitly with ``BQ_TELEMETRY_ENABLED=false`` (useful for tests,
    evaluation runs and offline development), and implicitly when no project
    can be resolved.
    """
    flag = os.getenv("BQ_TELEMETRY_ENABLED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(resolve_telemetry_project())


def build_bigquery_analytics_plugin():
    """Builds the BigQuery agent analytics plugin, or returns ``None``.

    Returns ``None`` — never raises — when telemetry is disabled, the
    environment is unconfigured, or the optional ``bigquery-analytics`` extra
    (pyarrow + BigQuery Storage Write API client) is not installed.
    """
    if not telemetry_enabled():
        logger.info(
            "BigQuery agent analytics disabled: set PROJECT_ID (or "
            "GOOGLE_CLOUD_PROJECT) and BQ_TELEMETRY_DATASET to enable."
        )
        return None

    try:
        from google.adk.plugins.bigquery_agent_analytics_plugin import (
            BigQueryAgentAnalyticsPlugin,
        )
    except ImportError as exc:
        logger.warning(
            "BigQuery agent analytics unavailable (%s). Install the extra with "
            "`uv pip install 'google-adk[bigquery-analytics]'`. The agent will "
            "run without telemetry.",
            exc,
        )
        return None

    project_id = resolve_telemetry_project()
    dataset_id = resolve_telemetry_dataset()
    location = resolve_telemetry_location()
    table_id = resolve_telemetry_table()

    try:
        plugin = BigQueryAgentAnalyticsPlugin(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            location=location,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break startup
        logger.warning(
            "Failed to initialise BigQuery agent analytics for %s.%s.%s in %s: %s",
            project_id,
            dataset_id,
            table_id,
            location,
            exc,
        )
        return None

    logger.info(
        "BigQuery agent analytics enabled -> %s.%s.%s (%s)",
        project_id,
        dataset_id,
        table_id,
        location,
    )
    return plugin


def build_plugins() -> list:
    """Returns the plugin list to hand to ``google.adk.apps.App``."""
    plugin = build_bigquery_analytics_plugin()
    return [plugin] if plugin is not None else []


__all__ = [
    "DEFAULT_TELEMETRY_DATASET",
    "DEFAULT_TELEMETRY_LOCATION",
    "DEFAULT_TELEMETRY_TABLE",
    "build_bigquery_analytics_plugin",
    "build_plugins",
    "resolve_telemetry_dataset",
    "resolve_telemetry_location",
    "resolve_telemetry_project",
    "resolve_telemetry_table",
    "telemetry_enabled",
]
