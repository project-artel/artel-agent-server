from fastapi import Request

from app.agents import ScenarioAgent
from app.llm import LLMClient


def get_llm_client(request: Request) -> LLMClient:
    """Return the process-wide LLM client wired in the app lifespan."""
    return request.app.state.llm_client


def get_scenario_agent(request: Request) -> ScenarioAgent:
    """Return the shared ScenarioAgent wired in the app lifespan."""
    return request.app.state.scenario_agent
