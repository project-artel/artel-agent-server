import uuid

from app.agents import AgentContext, ScenarioAgent
from app.agents.scenario_schemas import (
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioDraft,
)
from app.llm.client import LLMClient
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.schemas import LLMMessage, MessageRole
from app.sessions.schemas import SessionRecord
from app.sessions.store import SessionExpired, SessionStore


class SessionService:
    def __init__(
        self,
        store: SessionStore,
        llm_client: LLMClient,
        agent: ScenarioAgent | None = None,
        history_max_turns: int = 10,
    ) -> None:
        self._store = store
        self._llm = llm_client
        self._agent = agent or ScenarioAgent()
        # One turn == one user message + one assistant message.
        self._history_max_messages = history_max_turns * 2

    async def open(
        self,
        unity_context: dict,
        game_context: dict,
        user_input: str,
        model: LLMModel = DEFAULT_MODEL,
    ) -> str:
        session_id = uuid.uuid4().hex
        record = SessionRecord(
            unity_context=unity_context,
            game_context=game_context,
            pending_user_input=user_input,
            model=model,
        )
        await self._store.save(session_id, record)
        return session_id

    async def start_first_turn(self, session_id: str) -> ScenarioAgentResult | None:
        """Run the first generation using the input captured at open time.

        Returns None when there is no pending input (e.g. a reconnect after the
        first turn already ran).
        """
        record = await self._load(session_id)
        if not record.pending_user_input:
            return None

        user_input = record.pending_user_input
        record.pending_user_input = None
        result = await self._generate(session_id, record, user_input, draft=None)
        await self._store.save(session_id, record)
        return result

    async def run_turn(
        self,
        session_id: str,
        user_input: str,
        draft: ScenarioDraft | None,
        model: LLMModel | None = None,
    ) -> ScenarioAgentResult:
        record = await self._load(session_id)
        if model is not None:
            record.model = model
        result = await self._generate(session_id, record, user_input, draft)
        await self._store.save(session_id, record)
        return result

    async def close(self, session_id: str) -> None:
        await self._store.delete(session_id)

    async def _load(self, session_id: str) -> SessionRecord:
        record = await self._store.load(session_id)
        if record is None:
            raise SessionExpired(session_id)
        return record

    async def _generate(
        self,
        session_id: str,
        record: SessionRecord,
        user_input: str,
        draft: ScenarioDraft | None,
    ) -> ScenarioAgentResult:
        request = ScenarioAgentRequest(
            user_input=user_input,
            unity_context=record.unity_context,
            game_context=record.game_context,
            history=list(record.history),
            draft=draft,
            model=record.model,
        )
        context = AgentContext(session_id=session_id, llm=self._llm)
        result = await self._agent.run(request, context)

        record.history.append(
            LLMMessage(role=MessageRole.user, content=user_input)
        )
        record.history.append(
            LLMMessage(role=MessageRole.assistant, content=result.message)
        )
        if len(record.history) > self._history_max_messages:
            record.history = record.history[-self._history_max_messages :]
        return result
