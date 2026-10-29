"""Dropping the cached provider keys, model catalog and per-feature models on request.

The orchestration server calls this after an administrator saves or deletes a
key, switches a model on or off, or picks a model for a feature, so the change reaches the next model call
instead of waiting out the 30-second TTLs. Only this process drops its cache:
with several replicas behind one address, the others still expire on their TTL.
"""

from fastapi import APIRouter, Response, status

from app.llm.api_key import get_llm_api_key_provider
from app.llm.catalog import invalidate_model_catalog
from app.llm.model_settings import get_model_settings_provider

router = APIRouter(tags=["system"])


@router.post("/llm-cache/invalidate", status_code=status.HTTP_204_NO_CONTENT)
async def invalidate_llm_cache() -> Response:
    get_llm_api_key_provider().invalidate()
    invalidate_model_catalog()
    get_model_settings_provider().invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
