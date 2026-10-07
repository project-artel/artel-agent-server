"""The `load_skill` tool: a skill body the system prompt no longer carries.

From `qa_run/v18` on, the long sections of the system prompt live in
`qa_run/<version>/skill_<name>.md`, and the agent reads one when it needs it.
The tool only reads a prompt file. It sends nothing to the game and changes no
run state, so the result carries no scene view and no operator messages, the same
as the knowledge read tools (`knowledge_read_tools.py`).

The result is a `ToolMessage` like any other. Neither fold in
`app/agents/qa/context.py` touches it: they replace only the marked spans
`SceneMemory.render` and the knowledge search write, and a skill body carries
neither marker. `tests/test_qa_skill_tools.py` pins that. Compaction can still
summarise it away, which is why the description says to load again after a
compaction.

The tool is offered only when `arch.skills == "on_demand"`. With `off`,
`runner.system_prompt_with_skills` puts every skill body in the system prompt
instead, so there is nothing to load.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import PromptError, load_skill as load_skill_file
from app.prompts import load_tool_description, skill_names


def build_skill_tools(ctx: ToolContext, prompt_version: str | None = None) -> list[BaseTool]:
    """`load_skill`, bound to the skills of one `qa_run` prompt version.

    `prompt_version=None` resolves the way `load_prompt` does: the configured
    version, then the newest. The runner passes the run's own version so the
    description, the list of names and the bodies all come from the version
    `config.prompt_hashes` recorded.
    """
    names = skill_names(prompt_version)
    # Per run, because `build_tools` is called once per run. Kept here rather than
    # on `QaRunState`, since nothing outside this tool reads it.
    loaded: set[str] = set()

    @tool(
        description=load_tool_description("load_skill", prompt_version).body.format(
            skills=", ".join(names) or "(none in this prompt version)"
        )
    )
    async def load_skill(name: str, thought: str) -> str:
        # What the agent reads is `qa_run/<version>/tool_load_skill.md`, not this.
        wanted = (name or "").strip()
        if wanted not in names:
            valid = ", ".join(names) if names else "none"
            return f"{name!r} is not a skill, so nothing was loaded. Valid names: {valid}."
        try:
            body = load_skill_file(wanted, prompt_version).body
        except PromptError as error:
            return f"The skill {wanted!r} could not be read: {error}"
        if wanted in loaded:
            # Answered in full anyway: a reload usually means compaction removed
            # the first copy, and refusing would leave the agent without the text.
            return (
                f"(You already loaded {wanted!r} earlier in this run. Here it is "
                f"again.)\n\n{body}"
            )
        loaded.add(wanted)
        return body

    return [load_skill]
