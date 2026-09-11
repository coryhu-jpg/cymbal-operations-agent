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

"""POS Troubleshooting RAG Tool using BigQuery Vector Search and Sliding Window Chunks."""

import functools
import logging
import os
import re
import time

from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv()

logger = logging.getLogger(__name__)

BQ_LOCATION = os.getenv("BIGQUERY_LOCATION", "us-central1")
CHUNK_DATASET = os.getenv("POS_CHUNK_DATASET", "cymbal_gold")
CHUNK_TABLE_NAME = os.getenv("POS_CHUNK_TABLE", "pos_manual_chunk_embeddings")
MIN_SIMILARITY_THRESHOLD = float(os.getenv("POS_RAG_MIN_SIMILARITY", "0.70"))

# Canonical refusal string. Kept identical to the value published in
# tests/eval/contracts/pos_rag_contract.yaml so the contract, the agent prompt
# and the golden eval datasets cannot drift apart.
UNCERTIFIED_FALLBACK_MSG = (
    f"WARNING: No certified POS hardware documentation matched with confidence "
    f">= {MIN_SIMILARITY_THRESHOLD:.2f}. This inquiry appears out-of-scope for "
    f"Cymbal POS terminal runbooks."
)


@functools.cache
def resolve_project_id() -> str:
    """Resolves the Google Cloud project id from the environment or ADC.

    No project id is hardcoded, so the same artifact runs unmodified across
    dev, staging, prod and CI.
    """
    for var in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT"):
        value = os.getenv(var, "").strip()
        if value:
            return value

    try:
        import google.auth

        _, project = google.auth.default()
        if project:
            return project
    except Exception as exc:  # noqa: BLE001 - resolution must not crash import or tests
        logger.debug("ADC project resolution unavailable: %s", exc)

    logger.error(
        "Google Cloud project is not configured. Set GOOGLE_CLOUD_PROJECT or configure "
        "Application Default Credentials."
    )
    return ""


def resolve_chunk_table() -> str:
    """Returns the fully qualified embeddings table for vector search."""
    return f"{resolve_project_id()}.{CHUNK_DATASET}.{CHUNK_TABLE_NAME}"



def _gcs_to_https(uri: str) -> str:
    """Converts a gs:// URI to a clickable HTTPS URL."""
    if uri.startswith("gs://"):
        return uri.replace("gs://", "https://storage.cloud.google.com/")
    return uri


def _extract_error_codes(query: str) -> str | None:
    """Extracts hardware/payment error code tokens from query string."""
    matches = re.findall(r"[A-Za-z0-9]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", query)
    if matches:
        return f'"{matches[0]}"'
    return None


# Restated at the tool boundary rather than only in the agent instruction: the
# model reliably honours fidelity rules that sit immediately next to the content
# they govern, whereas the same rules buried in a long system prompt were
# repeatedly ignored (observed as grounding_v1 = 0.0 on runbook answers).
_FIDELITY_DIRECTIVE = """

---
#### MANDATORY RESPONSE CONSTRUCTION RULES (read before answering)
The steps above are certified manufacturer text. When you relay them to the user:
1. Copy each step VERBATIM. Keep the manual's own verbs and abbreviations ("3s", not "3 seconds"; "Open", not "Navigate to").
2. Do NOT prepend an invented label or title to a step. Never write "2. **Reboot Payment Module:** Hold Yellow + # ...". Write "2. Hold Yellow + # ...".
3. Do NOT add actions, objects, qualifiers, or timing words that are absent above (do not add "tap", "insert", "immediately", "right away").
4. Use the manual's name for the fault condition. If it is called a "Timeout", call it a Timeout - even if the user called it a freeze, hang, or crash.
5. For status-code branches, state ONLY the condition and the prescribed action. Do NOT explain what the status means (no "the transaction went through successfully", no "the charge was aborted").
6. Reproduce the document title and link exactly as given above.
7. If the user asked something these steps do not cover, say the certified documentation does not address it. Do NOT fill the gap from general knowledge.
"""


def _format_runbook(row, similarity: float, *, fallback: bool) -> str:
    """Render a retrieved runbook chunk plus the verbatim-reproduction directive."""
    doc_link = _gcs_to_https(row.source_pdf_uri)
    title_suffix = " (Fallback Exact Search)" if fallback else ""
    score_suffix = " (Keyword Match)" if fallback else ""
    return (
        f"### Certified POS Hardware Runbook: {row.document_title}{title_suffix}\n"
        f"- **Equipment Covered**: {row.equipment_covered}\n"
        f"- **Relevance Score**: {similarity:.4f}{score_suffix}\n"
        f"- **Certified Manual Link**: [{row.document_filename}]({doc_link})\n\n"
        f"#### Step-by-Step Field Recovery & Technical Guidance:\n"
        f"{row.stitched_content}"
        f"{_FIDELITY_DIRECTIVE}"
    )


def pos_troubleshooting_rag_tool(query: str) -> str:
    """Performs vector similarity search and full-text runbook retrieval over POS terminal technical manuals and hardware documentation.

    Use this tool for all hardware, terminal, device, mechanical, or equipment troubleshooting inquiries
    to search certified technical runbooks, retrieve step-by-step recovery procedures, or verify coverage.
    If the requested equipment is out of scope (e.g. non-POS hardware or vehicles), this tool returns a certified warning.

    Args:
        query: The hardware troubleshooting inquiry, error description, or error code.

    Returns:
        Formatted technical runbook extract with adjacent context window stitching,
        relevance score, and certified documentation links.
    """
    project_id = resolve_project_id()
    chunk_table = resolve_chunk_table()
    client = bigquery.Client(project=project_id, location=BQ_LOCATION)

    vector_sql = f"""
    WITH top_match AS (
      SELECT
        base.document_filename,
        base.document_title,
        base.equipment_covered,
        base.source_pdf_uri,
        base.chunk_index,
        base.chunk_content,
        ROUND(1 - distance, 4) AS similarity_score
      FROM VECTOR_SEARCH(
        TABLE `{chunk_table}`,
        'embedding',
        (SELECT AI.EMBED(@query, endpoint => 'text-embedding-005', task_type => 'RETRIEVAL_QUERY').result AS embedding),
        top_k => 1,
        distance_type => 'COSINE'
      )
    )
    SELECT
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.similarity_score,
      STRING_AGG(c.chunk_content, '\\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM top_match m
    JOIN `{chunk_table}` c
      ON m.document_filename = c.document_filename
      AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
    GROUP BY m.document_filename, m.document_title, m.equipment_covered, m.source_pdf_uri, m.similarity_score
    """

    for attempt in range(3):
        try:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("query", "STRING", query)],
                labels={"datacloud": "jetski"},
            )
            results = list(client.query(vector_sql, job_config=job_config, location=BQ_LOCATION).result())
            if results:
                row = results[0]
                similarity = float(row.similarity_score)
                if similarity >= MIN_SIMILARITY_THRESHOLD:
                    return _format_runbook(row, similarity, fallback=False)
            break
        except Exception:
            time.sleep(2 ** attempt)

    # Trigger SEARCH() fallback
    error_token = _extract_error_codes(query)
    search_term = error_token if error_token else f'"{query}"'

    search_sql = f"""
    WITH top_match AS (
      SELECT
        document_filename,
        document_title,
        equipment_covered,
        source_pdf_uri,
        chunk_index,
        chunk_content,
        0.85 AS similarity_score
      FROM `{chunk_table}`
      WHERE SEARCH(chunk_content, @search_term)
      LIMIT 1
    )
    SELECT
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.similarity_score,
      STRING_AGG(c.chunk_content, '\\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM top_match m
    JOIN `{chunk_table}` c
      ON m.document_filename = c.document_filename
      AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
    GROUP BY m.document_filename, m.document_title, m.equipment_covered, m.source_pdf_uri, m.similarity_score
    """

    for attempt in range(3):
        try:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("search_term", "STRING", search_term)],
                labels={"datacloud": "jetski"},
            )
            results = list(client.query(search_sql, job_config=job_config, location=BQ_LOCATION).result())
            if results:
                row = results[0]
                return _format_runbook(row, float(row.similarity_score), fallback=True)
            break
        except Exception:
            time.sleep(2 ** attempt)

    return UNCERTIFIED_FALLBACK_MSG

__all__ = [
    "pos_troubleshooting_rag_tool",
    "resolve_project_id",
    "resolve_chunk_table",
    "UNCERTIFIED_FALLBACK_MSG",
    "MIN_SIMILARITY_THRESHOLD",
]
