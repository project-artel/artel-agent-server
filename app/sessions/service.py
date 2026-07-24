import uuid

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agents import (
    DEFAULT_LANGUAGE,
    AgentContext,
    OutputLanguage,
    ScenarioAgent,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioDraft,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.sessions.schemas import HistoryTurn, SessionRecord
from app.sessions.store import SessionExpired, SessionStore


class SessionService:
    def __init__(
        self,
        store: SessionStore,
        agent: ScenarioAgent | None = None,
        history_max_turns: int = 10,
    ) -> None:
        self._store = store
        self._agent = agent or ScenarioAgent()
        # One turn == one user message + one assistant message.
        self._history_max_messages = history_max_turns * 2

    async def open(
        self,
        unity_context: dict,
        game_context: dict,
        user_input: str,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
    ) -> str:
        session_id = uuid.uuid4().hex
        record = SessionRecord(
            unity_context=unity_context,
            game_context=game_context,
            pending_user_input=user_input,
            model=model,
            language=language,
        )
        await self._store.save(session_id, record)
        return session_id

    async def start_first_turn(self, session_id: str) -> ScenarioAgentResult | None:
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
        language: OutputLanguage | None = None,
    ) -> ScenarioAgentResult:
        record = await self._load(session_id)
        if model is not None:
            record.model = model
        if language is not None:
            record.language = language
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

    def _replay_messages(self, record: SessionRecord) -> list[BaseMessage]:
        """Text-only messages for the prompt (scenarios are not replayed)."""
        window = record.history[-self._history_max_messages :]
        messages: list[BaseMessage] = []
        for turn in window:
            if turn.role == "user":
                messages.append(HumanMessage(turn.message))
            else:
                messages.append(AIMessage(turn.message))
        return messages

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
            history=self._replay_messages(record),
            draft=draft,
            model=record.model,
            language=record.language,
        )
        result = await self._agent.run(request, AgentContext(session_id=session_id))

        # Full store (assistant turn keeps the generated scenario).
        record.history.append(HistoryTurn(role="user", message=user_input))
        record.history.append(
            HistoryTurn(
                role="assistant",
                message=result.message,
                scenario=result.scenario,
            )
        )
        if len(record.history) > self._history_max_messages:
            record.history = record.history[-self._history_max_messages :]
        return result
