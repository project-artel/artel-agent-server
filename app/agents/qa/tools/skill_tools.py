"""The `load_skill` tool: a skill body the system prompt no longer carries.

From `qa_run/v19` on, the long sections of the system prompt live in
`qa_run/<version>/skill_<name>.md`, and the agent reads one when it needs it.
The tool only reads a prompt file. It sends nothing to the game and changes no
run state, so the result carries no scene view and no operator messages, the same
as the knowledge read tools (`knowledge_read_tools.py`).

The body comes back wrapped in `<<skill NAME>>` and `<<end skill NAME>>`, so
`fold_stale_skills` in `app/agents/qa/context.py` can find exactly that span and
replace it with a short note once a newer skill has been loaded. A skill body runs
to about 10,000 characters, and without the fold every one the run ever loaded
would be resent on every turn until compaction. The scene and knowledge folds do
not touch it: they replace only the spans `SceneMemory.render` and the knowledge
search mark, and a skill body carries neither. `tests/test_qa_skill_tools.py`
pins both directions. Compaction can still summarise a loaded skill away, which
is one more reason the description says to load a skill again when its rules are
no longer in front of the agent.

The tool is offered only when `arch.skills == "on_demand"`. With `off`,
`runner.system_prompt_with_skills` puts every skill body in the system prompt
instead, so there is nothing to load.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import PromptError, load_skill as load_skill_file
from app.agents.qa.arch import withheld_skills
from app.prompts import load_tool_description, skill_names

# The markers a returned skill body is wrapped in. The start marker carries the
# skill's name, the way a neighbour block's start marker carries the hit's id: a
# folded skill has to tell the agent which name to pass to `load_skill`. The end
# marker repeats the name so one skill's end can never close another's span.
SKILL_BLOCK_START_PREFIX = "<<skill "
SKILL_BLOCK_START_SUFFIX = ">>"
SKILL_BLOCK_END_PREFIX = "<<end skill "
SKILL_BLOCK_END_SUFFIX = ">>"


def wrap_skill(name: str, body: str) -> str:
    """`body` between the start and end markers for skill `name`."""
    return (
        f"{SKILL_BLOCK_START_PREFIX}{name}{SKILL_BLOCK_START_SUFFIX}\n{body}\n"
        f"{SKILL_BLOCK_END_PREFIX}{name}{SKILL_BLOCK_END_SUFFIX}"
    )


def build_skill_tools(ctx: ToolContext, prompt_version: str | None = None) -> list[BaseTool]:
    """`load_skill`, bound to the skills of one `qa_run` prompt version.

    `prompt_version=None` resolves the way `load_prompt` does: the configured
    version, then the newest. The runner passes the run's own version so the
    description, the list of names and the bodies all come from the version
    `config.prompt_hashes` recorded.
    """
    # 이 구조가 못 보는 skill 은 이름부터 없다. 모르는 이름으로 답하므로, 감춘 skill 을
    # 부르면 유효한 이름 목록이 돌아오고 거기에도 없다.
    withheld = withheld_skills(ctx.arch)
    names = tuple(name for name in skill_names(prompt_version) if name not in withheld)
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
        block = wrap_skill(wanted, body)
        if wanted in loaded:
            # Answered in full anyway: a reload usually means a fold or compaction
            # removed the first copy, and refusing would leave the agent without
            # the text. The note stays outside the markers, so a later fold
            # replaces only the body and the note still says this was a reload.
            return (
                f"(You already loaded {wanted!r} earlier in this run. Here it is "
                f"again.)\n\n{block}"
            )
        loaded.add(wanted)
        return block

    return [load_skill]
