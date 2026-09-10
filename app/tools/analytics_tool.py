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

"""NL2SQL Data Agent Tool for Cymbal Operations Agent leveraging BigQuery Conversational Data Agent."""

import logging
import os
import time
from typing import Any

import google.auth
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent

logger = logging.getLogger(__name__)

DEFAULT_DATA_AGENT_NAME = (
    "projects/project-elevate-data-advance/locations/global/dataAgents/gda-f3ad9f8f-c345-4e25-92d7-3e8f2f29d9c5"
)
FALLBACK_UNREACHABLE_MSG = (
    "Store operational data is temporarily unreachable. Please verify BigQuery connectivity or retry shortly."
)


def _format_data_agent_result(res: dict[str, Any]) -> str:
    """Formats the Data Agent response into readable markdown with data and SQL."""
    parts = []
    final_response = []
    sql = None
    data_table = None

    for step in res.get("response", []):
        if "text" in step and step["text"].get("textType") == "FINAL_RESPONSE":
            final_response.extend(step["text"].get("parts", []))
        if "data" in step and "generatedSql" in step["data"]:
            sql = step["data"]["generatedSql"]
        if "Data Retrieved" in step:
            dr = step["Data Retrieved"]
            headers = dr.get("headers", [])
            rows = dr.get("rows", [])
            summary = dr.get("summary", "")
            if headers and rows:
                md_rows = [" | ".join(str(h) for h in headers)]
                md_rows.append(" | ".join(["---"] * len(headers)))
                for r in rows[:25]:
                    md_rows.append(" | ".join(str(c) for c in r))
                data_table = "\n".join(md_rows)
                if summary:
                    data_table += f"\n\n*{summary}*"

    if final_response:
        parts.append("\n".join(final_response))
    if data_table:
        parts.append(f"\n### Retrieved Store Data\n{data_table}")
    if sql:
        parts.append(f"\n### Generated GoogleSQL\n```sql\n{sql}\n```")

    if not parts:
        return "Query processed successfully, but no data records were returned."
    return "\n\n".join(parts)


def cymbal_analytics_tool(query: str) -> str:
    """Queries enterprise store data, inventory reconciliation, warranties, and sales transactions using BigQuery NL2SQL.

    Use this tool for analytical and transactional store queries, including:
    - Estimating cover hours remaining and stockout risk for store inventory positions
    - Total on-hand inventory across stores and warehouses
    - Looking up sales transaction details, line items, customer loyalty tier, and payment methods
    - Retrieving product warranty coverage policies, service levels, defect protection, and exclusions
    - Historical cashier override baselines (e.g. 7-day override baseline) and cashier discount averages
    - Ranking top cashiers with cashier promo abuse alerts over the last 7 days

    Args:
        query: Verbatim natural language inquiry referencing standardized enterprise business terms
               (e.g., Net Transaction Revenue, Total On-Hand Inventory, Estimated Cover Hours,
               Cashier Manual Override Rate). Pass terms verbatim without keyword stripping.

    Returns:
        Analysis summary, retrieved data records, and executed GoogleSQL query.
    """
    data_agent_name = os.getenv("DATA_AGENT_NAME", DEFAULT_DATA_AGENT_NAME)
    creds, _ = google.auth.default()
    settings = DataAgentToolConfig()

    max_attempts = 3
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("Calling Data Agent (attempt %d/%d): %s", attempt, max_attempts, query)
            res = ask_data_agent(
                data_agent_name=data_agent_name,
                query=query,
                credentials=creds,
                settings=settings,
                tool_context=None,
            )
            if res.get("status") == "SUCCESS":
                return _format_data_agent_result(res)
            logger.warning("Data Agent returned non-success status: %s", res)
            last_err = res.get("status", "Unknown error")
        except Exception as e:
            logger.warning("Data Agent call failed on attempt %d: %s", attempt, e)
            last_err = e

        if attempt < max_attempts:
            time.sleep(2 ** (attempt - 1))

    logger.error("All Data Agent attempts failed. Last error: %s", last_err)
    return FALLBACK_UNREACHABLE_MSG


__all__ = ["cymbal_analytics_tool"]
