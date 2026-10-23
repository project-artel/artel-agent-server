import base64
import re
from collections.abc import Awaitable, Callable

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.config import get_settings
from app.documents.fetch import fetch_document
from app.demonstrations.schemas import ExtractionRequest, ExtractionResponse, VerificationProcedure
from app.llm.chat_model import structured
from app.llm.models import DEFAULT_MODEL
from app.prompts import load_prompt


async def download_evidence(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        evidence = await fetch_document(
            url, client=client, max_bytes=2097152, timeout=15,
            allowed_hosts=get_settings().extract_allowed_hosts,
        )
    return evidence.data


class DemonstrationExtractionAgent:
    def __init__(
        self, structured_factory: Callable[[], Runnable] | None = None,
        image_loader: Callable[[str], Awaitable[bytes]] | None = None,
    ):
        self._factory = structured_factory or (lambda: structured(DEFAULT_MODEL, VerificationProcedure))
        self._image_loader = image_loader or download_evidence

    async def run(self, request: ExtractionRequest) -> ExtractionResponse:
        prompt = load_prompt("demonstration", "system", "v1")
        content = [{"type": "text", "text": request.model_dump_json(exclude={"images"})}]
        for image in request.images:
            # 내부 저장소 URL을 provider가 열 수 없으므로 서버에서 제한된 크기로 읽는다.
            raw = await self._image_loader(str(image.url))
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 2097152:
                raise ValueError("Demonstration evidence must be a PNG of at most 2 MiB")
            encoded = base64.b64encode(raw).decode("ascii")
            content.extend([
                {"type": "text", "text": f"Source image for evidence event {image.eventId}:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ])
        context = AgentContext(
            session_id=f"demonstration-{request.demonstrationId}",
            metadata={"source_hash": request.sourceHash, "prompt_version": prompt.version,
                      "prompt_sha256": prompt.body_sha256},
        )
        # `structured` 호출에는 게임 조작 도구나 QA 실행 상태를 연결하지 않는다.
        answer = await self._factory().ainvoke(
            [SystemMessage(content=prompt.body), HumanMessage(content=content)],
            context.trace_config("demonstration-extraction"),
        )
        procedure = VerificationProcedure.model_validate(answer)
        event_ids = {event.id for event in request.events}
        for item in [*procedure.steps, *procedure.assertions]:
            if not set(item.evidenceEventIds).issubset(event_ids):
                raise ValueError("Procedure references an unknown evidence event")
        # TC ID를 `event` ID로 잘못 인용하면 존재하지 않는 근거로 승인되는 것을 막는다.
        for statement in [*procedure.facts, *procedure.inferences, *procedure.preconditions]:
            citations = re.findall(r"(?:\bevents?|이벤트)\s*#?\s*(\d+)", statement, re.IGNORECASE)
            if any(int(citation) not in event_ids for citation in citations):
                raise ValueError("Procedure text cites an unknown evidence event")
        return ExtractionResponse(sourceHash=request.sourceHash, procedure=procedure)
