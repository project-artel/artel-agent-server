import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(content: str) -> str:
    """Best-effort extraction of a JSON object from an LLM response.

    Strips a markdown code fence if present, then slices from the first ``{``
    to the last ``}``. Raises ValueError when no object-like span is found.
    """
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in content.")
    return text[start : end + 1]
