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
    ScenarioPlan,
    AuthoredFlow,
    TestCaseListItem,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.usage import set_usage_scope
from app.sessions.channel import ScenarioChannel
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
        test_case_list: list[TestCaseListItem] | None = None,
        flows: list[AuthoredFlow] | None = None,
        entry_scene: str | None = None,
        model: LLMModel = DEFAULT_MODEL,
        locale: OutputLanguage = DEFAULT_LANGUAGE,
        test_scenario_id: int | None = None,
        run_id: int | None = None,
        project_id: int | None = None,
        current_scenarios: list[ScenarioPlan] | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        record = SessionRecord(
            unity_context=unity_context,
            game_context=game_context,
            test_case_list=test_case_list or [],
            flows=flows or [],
            entry_scene=entry_scene,
            pending_user_input=user_input,
            model=model,
            locale=locale,
            test_scenario_id=test_scenario_id,
            run_id=run_id,
            project_id=project_id,
            current_scenarios=current_scenarios or [],
        )
        await self._store.save(session_id, record)
        return session_id

    async def start_first_turn(
        self, session_id: str, channel: ScenarioChannel
    ) -> ScenarioAgentResult | None:
        record = await self._load(session_id)
        if not record.pending_user_input:
            return None

        user_input = record.pending_user_input
        record.pending_user_input = None
        result = await self._generate(
            session_id, record, user_input, draft=None, channel=channel
        )
        await self._store.save(session_id, record)
        return result

    async def run_turn(
        self,
        session_id: str,
        channel: ScenarioChannel,
        user_input: str,
        draft: ScenarioDraft | None,
        model: LLMModel | None = None,
        locale: OutputLanguage | None = None,
        current_scenarios: list[ScenarioPlan] | None = None,
    ) -> ScenarioAgentResult:
        record = await self._load(session_id)
        if model is not None:
            record.model = model
        if locale is not None:
            record.locale = locale
        # Orchestration refreshes the run's current scenarios each turn, so the
        # agent always sees the latest state (prior turns' adds/edits applied).
        if current_scenarios is not None:
            record.current_scenarios = current_scenarios
        result = await self._generate(
            session_id, record, user_input, draft, channel=channel
        )
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
        channel: ScenarioChannel,
    ) -> ScenarioAgentResult:
        # `run_id`, not `test_scenario_id`: Orchestration opens this session from a
        # run and sends `project_id`/`run_id` only (ARTEL-206 Step 6), so
        # `test_scenario_id` is always None here and every scenario call was
        # landing with a null reference — spend nothing could account for.
        # `test_run.id` is also what the receiving side reads `SCENARIO` as
        # (`LlmUsageServiceType`).
        set_usage_scope("SCENARIO", record.run_id)
        request = ScenarioAgentRequest(
            user_input=user_input,
            unity_context=record.unity_context,
            game_context=record.game_context,
            test_case_list=record.test_case_list,
            flows=record.flows,
            entry_scene=record.entry_scene,
            history=self._replay_messages(record),
            draft=draft,
            current_scenarios=record.current_scenarios,
            model=record.model,
            locale=record.locale,
            run_id=record.run_id,
        )
        result = await self._agent.run(
            request, AgentContext(session_id=session_id), channel
        )

        # Full store (assistant turn keeps the scenarios it authored).
        record.history.append(HistoryTurn(role="user", message=user_input))
        record.history.append(
            HistoryTurn(
                role="assistant",
                message=result.message,
                scenarios=result.scenarios,
            )
        )
        if len(record.history) > self._history_max_messages:
            record.history = record.history[-self._history_max_messages :]
        return result
