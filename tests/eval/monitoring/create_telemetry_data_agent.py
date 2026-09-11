"""Create the BigQuery Conversational Analytics Data Agent for agent telemetry.

Lab 04 Part 4.1. Scopes a Data Agent to the `agent_telemetry` dataset and seeds
it with the four monitoring recipes the lab asks for. Each example query was
executed against BigQuery first, so the SQL committed here is known-good rather
than aspirational.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT = "project-elevate-data-advance"
LOCATION = "global"
AGENT_ID = "cymbal-telemetry-monitoring-agent"
DATASET = "agent_telemetry"

BASE = (
    f"https://geminidataanalytics.googleapis.com/v1beta/"
    f"projects/{PROJECT}/locations/{LOCATION}/dataAgents"
)

SYSTEM_INSTRUCTION = (
    "You are the Cymbal Operations Agent telemetry analyst. You answer questions "
    "about the runtime behaviour, cost and reliability of the deployed "
    "cymbal_operations_agent by querying its BigQuery Agent Analytics tables in "
    "the agent_telemetry dataset.\n"
    "\n"
    "Data model:\n"
    "- `events` is the raw append-only event stream, partitioned by `timestamp` "
    "and clustered by `event_type, agent, user_id`. ALWAYS apply a `timestamp` "
    "filter so queries prune partitions.\n"
    "- Typed views project one event type each. Prefer them over `events`:\n"
    "  * `v_llm_response` - token usage and model latency. Columns include "
    "`model_version`, `usage_prompt_tokens`, `usage_completion_tokens`, "
    "`usage_total_tokens`, `usage_cached_tokens`, `usage_thinking_tokens`, "
    "`total_ms`, `ttft_ms`, `finish_reason`.\n"
    "  * `v_tool_completed` - successful tool calls. Columns include "
    "`tool_name`, `total_ms`, `tool_origin`, `function_call_id`.\n"
    "  * `v_tool_error`, `v_llm_error`, `v_invocation_error`, `v_agent_error` - "
    "failures, one view per layer.\n"
    "  * `v_invocation_starting` / `v_invocation_completed` - invocation "
    "boundaries. `v_user_message_received` - inbound user turns.\n"
    "\n"
    "Cost conventions: report Gemini 3.6 Flash spend as "
    "prompt_tokens/1e6 * 0.30 USD plus completion_tokens/1e6 * 2.50 USD, and "
    "label the result an estimate.\n"
    "Latency conventions: `total_ms` is milliseconds; convert to seconds when "
    "presenting to a human, and report average, p95 and max together rather "
    "than average alone, because tool latency here is heavily right-skewed."
)

EXAMPLE_QUERIES = [
    {
        "naturalLanguageQuestion": (
            "How many tokens has each model consumed and what did it cost?"
        ),
        "sqlQuery": f"""SELECT
  model_version,
  COUNT(*) AS llm_calls,
  SUM(usage_prompt_tokens) AS prompt_tokens,
  SUM(usage_completion_tokens) AS completion_tokens,
  SUM(usage_total_tokens) AS total_tokens,
  ROUND(SUM(usage_prompt_tokens) / 1e6 * 0.30, 4) AS est_input_usd,
  ROUND(SUM(usage_completion_tokens) / 1e6 * 2.50, 4) AS est_output_usd,
  ROUND(SUM(usage_prompt_tokens) / 1e6 * 0.30
        + SUM(usage_completion_tokens) / 1e6 * 2.50, 4) AS est_total_usd
FROM `{PROJECT}.{DATASET}.v_llm_response`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY model_version
ORDER BY est_total_usd DESC;""",
    },
    {
        "naturalLanguageQuestion": (
            "What is the average and maximum latency for each tool?"
        ),
        "sqlQuery": f"""SELECT
  tool_name,
  COUNT(*) AS calls,
  ROUND(AVG(total_ms) / 1000, 2) AS avg_seconds,
  ROUND(APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] / 1000, 2) AS p95_seconds,
  ROUND(MAX(total_ms) / 1000, 2) AS max_seconds
FROM `{PROJECT}.{DATASET}.v_tool_completed`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY tool_name
ORDER BY avg_seconds DESC;""",
    },
    {
        "naturalLanguageQuestion": (
            "How many tool calls failed, and how many sessions were affected?"
        ),
        "sqlQuery": f"""SELECT
  tool_name,
  COUNT(*) AS failed_calls,
  COUNT(DISTINCT session_id) AS affected_sessions,
  COUNT(DISTINCT invocation_id) AS affected_invocations,
  ARRAY_AGG(DISTINCT error_message IGNORE NULLS LIMIT 5) AS sample_errors
FROM `{PROJECT}.{DATASET}.v_tool_error`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY tool_name
ORDER BY failed_calls DESC;""",
    },
    {
        "naturalLanguageQuestion": "Which three tools are used the most?",
        "sqlQuery": f"""SELECT
  tool_name,
  COUNT(*) AS invocations,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_all_tool_calls
FROM `{PROJECT}.{DATASET}.v_tool_completed`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY tool_name
ORDER BY invocations DESC
LIMIT 3;""",
    },
]

TABLES = ["events", "v_llm_response", "v_tool_completed", "v_tool_error"]


def token() -> str:
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def request(method: str, url: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("X-Goog-User-Project", PROJECT)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main() -> int:
    payload = {
        "displayName": "Cymbal Agent Telemetry Monitoring Agent",
        "description": (
            "Conversational analytics over BigQuery Agent Analytics telemetry for "
            "the deployed cymbal_operations_agent: token cost, tool latency, "
            "failures and tool-usage distribution."
        ),
        "dataAnalyticsAgent": {
            "publishedContext": {
                "systemInstruction": SYSTEM_INSTRUCTION,
                "exampleQueries": EXAMPLE_QUERIES,
                "datasourceReferences": {
                    "bq": {
                        "tableReferences": [
                            {
                                "projectId": PROJECT,
                                "datasetId": DATASET,
                                "tableId": table,
                            }
                            for table in TABLES
                        ]
                    }
                },
                "options": {"analysis": {"python": {"enabled": True}}},
            }
        },
    }

    status, text = request("POST", f"{BASE}?dataAgentId={AGENT_ID}", payload)
    if status >= 400 and "ALREADY_EXISTS" in text:
        print("Agent already exists; patching instead.")
        status, text = request(
            "PATCH",
            f"{BASE}/{AGENT_ID}?updateMask=displayName,description,dataAnalyticsAgent",
            payload,
        )

    print(f"HTTP {status}")
    try:
        parsed = json.loads(text)
        print(json.dumps(parsed, indent=2)[:1200])
    except json.JSONDecodeError:
        print(text[:1200])
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
