"""Dropping the cached provider keys and model catalog on request.

The orchestration server calls this after an administrator saves or deletes a
key or switches a model on or off, so the change reaches the next model call
instead of waiting out the 30-second TTLs. Only this process drops its cache:
with several replicas behind one address, the others still expire on their TTL.
"""

from fastapi import APIRouter, Response, status

from app.llm.api_key import get_llm_api_key_provider
from app.llm.catalog import invalidate_model_catalog

router = APIRouter(tags=["system"])


@router.post("/llm-cache/invalidate", status_code=status.HTTP_204_NO_CONTENT)
async def invalidate_llm_cache() -> Response:
    get_llm_api_key_provider().invalidate()
    invalidate_model_catalog()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
