from typing import List, Dict, Any
import logging
import hashlib

from api.config import settings
from api.simple_http import AsyncSimpleClient, HTTPStatusError, RequestError


logger = get_logger = logging.getLogger(__name__)

# Use a dedicated v2 Vectorize index for transcript chunks.
VECTORIZE_INDEX_NAME = "quill-transcripts-v2"


def _vectorize_base_url() -> str:
    if not settings.cloudflare_account_id:
        logger.error(
            "vectorize_missing_account_id",
            extra={"has_account_id": bool(settings.cloudflare_account_id)},
        )
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is required for Vectorize operations")
    return f"https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}"


def _auth_headers() -> Dict[str, str]:
    token = getattr(settings, "cloudflare_api_token", None) or getattr(settings, "CLOUDFLARE_API_TOKEN", None)
    if not token:
        logger.error(
            "vectorize_missing_api_token",
            extra={
                "has_cloudflare_api_token": bool(getattr(settings, "cloudflare_api_token", None)),
                "has_CLOUDFLARE_API_TOKEN": bool(getattr(settings, "CLOUDFLARE_API_TOKEN", None)),
            },
        )
        raise RuntimeError("CLOUDFLARE_API_TOKEN is required for Vectorize operations")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def store_embeddings(
    *,
    vectors: List[List[float]],
    metadatas: List[Dict[str, Any]],
) -> int:
    """Upsert a batch of vectors into Cloudflare Vectorize.

    Each vector is paired with a metadata dict. We derive a stable ID from
    project/document/chunk metadata so repeated upserts are idempotent.
    """
    if not vectors:
        return 0
    if len(vectors) != len(metadatas):
        raise ValueError("vectors and metadatas length mismatch")

    base_url = _vectorize_base_url()
    client = AsyncSimpleClient(base_url=base_url, timeout=20.0)
    headers = _auth_headers()

    payload_vectors: List[Dict[str, Any]] = []
    for vec, meta in zip(vectors, metadatas):
        meta = dict(meta or {})
        project_id = meta.get("project_id")
        document_id = meta.get("document_id")
        chunk_index = meta.get("chunk_index")

        if project_id is None or document_id is None or chunk_index is None:
            raise ValueError(
                f"Missing required metadata for vector ID: project_id={project_id!r} "
                f"document_id={document_id!r} chunk_index={chunk_index!r}"
            )

        raw_id = meta.get("id")
        if raw_id is not None:
            base_id = str(raw_id)
        else:
            base_id = f"{project_id}:{document_id}:{chunk_index}"

        # Vectorize requires IDs to be at most 64 bytes. If our concatenated
        # identifier is longer, hash it down to a deterministic SHA-1 hex.
        encoded = base_id.encode("utf-8")
        if len(encoded) > 64:
            vec_id = hashlib.sha1(encoded).hexdigest()
        else:
            vec_id = base_id
        payload_vectors.append(
            {
                "id": vec_id,
                "values": vec,
                "metadata": meta,
            }
        )

    body: Dict[str, Any] = {"vectors": payload_vectors}
    # Use the Vectorize v2 REST API for upsert operations.
    endpoint_path = f"/vectorize/v2/indexes/{VECTORIZE_INDEX_NAME}/upsert"

    logger.info(
        "vectorize_upsert_request",
        extra={"index": VECTORIZE_INDEX_NAME, "count": len(payload_vectors)},
    )

    try:
        response = await client.post(endpoint_path, headers=headers, json=body)
        response.raise_for_status()
    except HTTPStatusError as exc:
        body_preview = None
        try:
            body_preview = getattr(exc.response, "text", None)
        except Exception:
            body_preview = None
        logger.error(
            "vectorize_upsert_http_error",
            exc_info=True,
            extra={
                "status_code": getattr(exc.response, "status_code", None),
                "body": body_preview,
            },
        )
        raise
    except RequestError as exc:
        logger.error("vectorize_upsert_request_error", exc_info=True, extra={"error": str(exc)})
        raise
    except Exception:
        logger.error("vectorize_upsert_unexpected_error", exc_info=True)
        raise

    # Vectorize returns a standard Cloudflare API envelope; treat HTTP 2xx as success.
    return len(payload_vectors)


async def query_project_chunks(
    *,
    project_id: str,
    query_vector: List[float],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Query the Vectorize index for the best-matching chunks within a project.

    Returns a list of match dicts containing at least metadata and score.
    """
    if not query_vector:
        return []

    base_url = _vectorize_base_url()
    client = AsyncSimpleClient(base_url=base_url, timeout=10.0)
    headers = _auth_headers()

    body: Dict[str, Any] = {
        "vector": query_vector,
        # Vectorize v2 expects camelCase topK and supports returning metadata.
        "topK": int(limit or 5),
        "returnMetadata": "all",
        # NOTE: We intentionally omit server-side metadata filters for now to
        # avoid requiring a metadata index on project_id. The API layer still
        # restricts results to the given project_id when joining with
        # transcript_chunks.
    }

    # Use the Vectorize v2 REST API for query operations.
    endpoint_path = f"/vectorize/v2/indexes/{VECTORIZE_INDEX_NAME}/query"

    logger.info(
        "vectorize_query_request",
        extra={"index": VECTORIZE_INDEX_NAME, "project_id": project_id, "limit": limit},
    )

    try:
        response = await client.post(endpoint_path, headers=headers, json=body)
        response.raise_for_status()
    except HTTPStatusError as exc:
        body_preview = None
        try:
            body_preview = getattr(exc.response, "text", None)
        except Exception:
            body_preview = None
        logger.error(
            "vectorize_query_http_error",
            exc_info=True,
            extra={
                "status_code": getattr(exc.response, "status_code", None),
                "body": body_preview,
            },
        )
        raise
    except RequestError as exc:
        logger.error("vectorize_query_request_error", exc_info=True, extra={"error": str(exc)})
        raise
    except Exception:
        logger.error("vectorize_query_unexpected_error", exc_info=True)
        raise

    try:
        data: Dict[str, Any] = response.json()
    except Exception:
        logger.error("vectorize_query_invalid_json", exc_info=True)
        raise

    # Temporary: log raw result structure (without truncating) to understand
    # how matches and metadata are shaped in the current Vectorize v2 API.
    try:
        logger.info(
            "vectorize_query_raw_result",
            extra={
                "has_result": isinstance(data.get("result"), dict),
                "top_level_keys": list(data.keys()),
                "result_keys": list((data.get("result") or {}).keys()),
                "matches_count": len((data.get("result") or {}).get("matches") or data.get("matches") or []),
            },
        )
    except Exception:
        # Logging must never break query path
        logger.warning("vectorize_query_raw_result_logging_failed", exc_info=True)

    # Newer Vectorize APIs typically wrap matches under result.matches
    result = data.get("result") or {}
    matches = result.get("matches") or data.get("matches") or []
    out: List[Dict[str, Any]] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "id": m.get("id"),
                "score": m.get("score"),
                "metadata": m.get("metadata") or {},
            }
        )
    return out
