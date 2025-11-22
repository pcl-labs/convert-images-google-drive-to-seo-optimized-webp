from typing import List, Dict, Any
import logging

from api.config import settings
from api.simple_http import AsyncSimpleClient, HTTPStatusError, RequestError


logger = logging.getLogger(__name__)

# Default embedding model routed via Cloudflare AI Gateway "openai" provider.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts using Cloudflare AI Gateway (OpenAI compat).

    This mirrors the gateway usage pattern from ai_modules.compose_blog_from_text,
    but targets the /embeddings endpoint instead of /chat/completions.
    """
    # Normalize inputs and short-circuit on empty
    clean_inputs: List[str] = [str(t or "").strip() for t in texts or []]
    if not clean_inputs:
        return []

    if not settings.cloudflare_account_id or not getattr(settings, "cf_ai_gateway_token", None):
        logger.error(
            "embeddings_ai_gateway_missing_config",
            extra={
                "has_account_id": bool(settings.cloudflare_account_id),
                "has_token": bool(getattr(settings, "cf_ai_gateway_token", None)),
            },
        )
        raise RuntimeError("Cloudflare AI Gateway configuration is missing for embeddings")

    model_name = DEFAULT_EMBEDDING_MODEL
    gateway_base = f"https://gateway.ai.cloudflare.com/v1/{settings.cloudflare_account_id}/quill/openai"
    endpoint_path = "/embeddings"
    client = AsyncSimpleClient(base_url=gateway_base, timeout=20.0)

    logger.info(
        "embed_texts_request",
        extra={
            "model": model_name,
            "num_texts": len(clean_inputs),
            "openai_api_base": getattr(settings, "openai_api_base", None),
            "ai_gateway_token_set": bool(getattr(settings, "cf_ai_gateway_token", None)),
        },
    )

    try:
        response = await client.post(
            endpoint_path,
            headers={
                "Content-Type": "application/json",
                "cf-aig-authorization": f"Bearer {settings.cf_ai_gateway_token}",
            },
            json={
                "model": model_name,
                "input": clean_inputs,
            },
        )
        response.raise_for_status()
    except HTTPStatusError as exc:
        logger.error(
            "embed_texts_http_error",
            exc_info=True,
            extra={"status_code": exc.response.status_code},
        )
        raise
    except RequestError as exc:
        logger.error("embed_texts_request_error", exc_info=True, extra={"error": str(exc)})
        raise
    except Exception:
        logger.error("embed_texts_unexpected_error", exc_info=True)
        raise

    try:
        data: Dict[str, Any] = response.json()
    except Exception:
        logger.error("embed_texts_invalid_json", exc_info=True)
        raise

    items = data.get("data") or []
    if not isinstance(items, list):
        logger.error("embed_texts_missing_data_field", extra={"keys": list(data.keys())})
        raise RuntimeError("Embeddings response missing 'data' list")

    # Each item is expected to have an "embedding" field (OpenAI compat schema).
    vectors: List[List[float]] = []
    for idx, item in enumerate(items):
        emb = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(emb, list):
            logger.warning("embed_texts_item_missing_embedding", extra={"index": idx})
            continue
        vectors.append(emb)

    # Preserve input cardinality where possible; if fewer vectors are returned
    # than inputs, callers should treat this as an error.
    if len(vectors) != len(clean_inputs):
        logger.warning(
            "embed_texts_length_mismatch",
            extra={"inputs": len(clean_inputs), "embeddings": len(vectors)},
        )

    return vectors
