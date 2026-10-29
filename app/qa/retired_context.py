"""과거 Redis QA snapshot에서 폐기한 시연 context만 삭제한다."""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


_REPLACE_UNCHANGED = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'XX', 'KEEPTTL')
return 1
"""


def without_demonstration_context(raw: str | bytes) -> str | None:
    value = json.loads(raw)
    changed = False
    for item in value.get("scenarios", []):
        for step in item.get("scenario", {}).get("steps", []):
            case = step.get("case")
            if isinstance(case, dict) and "verification_procedure" in case:
                del case["verification_procedure"]
                changed = True
    hashes = value.get("run_config", {}).get("prompt_hashes", {})
    for key in list(hashes):
        if key.startswith("demonstration/"):
            del hashes[key]
            changed = True
    return json.dumps(value, ensure_ascii=False) if changed else None


async def purge_demonstration_context(redis: "Redis") -> None:
    # Redis의 QA 세션 namespace만 순회한다. 세션 자체와 TTL은 유지한다.
    async for key in redis.scan_iter(match="qa:*", count=100):
        for _ in range(3):
            raw = await redis.get(key)
            if raw is None:
                break
            replacement = without_demonstration_context(raw)
            if replacement is None:
                break
            if await redis.eval(_REPLACE_UNCHANGED, 1, key, raw, replacement):
                break
        else:
            raise RuntimeError("QA session context cleanup conflicted with concurrent updates")
