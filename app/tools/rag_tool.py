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

import os
import re
import time

from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "project-elevate-data-advance")
BQ_LOCATION = os.getenv("BIGQUERY_LOCATION", "us-central1")
CHUNK_TABLE = f"{PROJECT_ID}.cymbal_gold.pos_manual_chunk_embeddings"
MIN_SIMILARITY_THRESHOLD = 0.70


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
    client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

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
        TABLE `{CHUNK_TABLE}`,
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
    JOIN `{CHUNK_TABLE}` c
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
                    doc_link = _gcs_to_https(row.source_pdf_uri)
                    return (
                        f"### Certified POS Hardware Runbook: {row.document_title}\n"
                        f"- **Equipment Covered**: {row.equipment_covered}\n"
                        f"- **Relevance Score**: {similarity:.4f}\n"
                        f"- **Certified Manual Link**: [{row.document_filename}]({doc_link})\n\n"
                        f"#### Step-by-Step Field Recovery & Technical Guidance:\n"
                        f"{row.stitched_content}"
                    )
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
      FROM `{CHUNK_TABLE}`
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
    JOIN `{CHUNK_TABLE}` c
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
                doc_link = _gcs_to_https(row.source_pdf_uri)
                return (
                    f"### Certified POS Hardware Runbook: {row.document_title} (Fallback Exact Search)\n"
                    f"- **Equipment Covered**: {row.equipment_covered}\n"
                    f"- **Relevance Score**: {float(row.similarity_score):.4f} (Keyword Match)\n"
                    f"- **Certified Manual Link**: [{row.document_filename}]({doc_link})\n\n"
                    f"#### Step-by-Step Field Recovery & Technical Guidance:\n"
                    f"{row.stitched_content}"
                )
            break
        except Exception:
            time.sleep(2 ** attempt)

    return (
        "Warning: No certified POS hardware documentation found with sufficient relevance "
        "(similarity score < 0.70). This inquiry appears out-of-scope for Cymbal POS terminal runbooks."
    )
