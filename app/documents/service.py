"""Stateless extraction: source URL → GameContext.

Composes fetch → loader → agent. Owns no persistence, dedup, or project
concept — those live in orchestration. This is the whole of agent-server's KB
responsibility.
"""

import uuid
from collections.abc import Sequence

import httpx

from app.agents import AgentContext, GameContext, GameContextAgent
from app.agents.game_context import GameContextAgentRequest
from app.documents.fetch import fetch_document
from app.documents.loader import extract_document_text
from app.llm.models import DEFAULT_MODEL, LLMModel


class ExtractionService:
    def __init__(
        self,
        agent: GameContextAgent,
        http_client: httpx.AsyncClient,
        *,
        max_bytes: int,
        timeout: float,
        allowed_hosts: Sequence[str] = (),
    ) -> None:
        self._agent = agent
        self._client = http_client
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._allowed_hosts = tuple(allowed_hosts)

    async def extract(
        self,
        source_url: str,
        filename: str,
        model: LLMModel = DEFAULT_MODEL,
    ) -> GameContext:
        fetched = await fetch_document(
            source_url,
            client=self._client,
            max_bytes=self._max_bytes,
            timeout=self._timeout,
            allowed_hosts=self._allowed_hosts,
        )
        text = extract_document_text(
            fetched.data, filename=filename, content_type=fetched.content_type
        )
        request = GameContextAgentRequest(document_text=text, model=model)
        # session_id is correlation-only; extraction is stateless.
        context = AgentContext(session_id=f"extract-{uuid.uuid4().hex}")
        return await self._agent.run(request, context)
