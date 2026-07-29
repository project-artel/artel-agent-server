from app.agents.scenario import OutputLanguage


# Written in the target language on purpose (see scenario.prompt for the rationale).
# Kept in Python rather than in a prompt file: it is keyed by OutputLanguage, and
# a file could lose a member without anything noticing.
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: "모든 자연어 출력(thought, message, reasoning)을 한국어로 작성한다.",
    OutputLanguage.en: "Write every natural-language output (thought, message, reasoning) in English.",
}
