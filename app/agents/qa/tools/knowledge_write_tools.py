"""지식창고를 고치는 도구 다섯 개.

읽기와 갈라 둔 이유는 예산이다. 쓰기는 런마다 횟수가 정해져 있고, 지우기는 그중 가장
적은 몫을 받는다. 고치는 것이 지우고 다시 쓰는 것보다 나은 이유는
`app/agents/qa/knowledge.py` 의 모듈 설명에 적혀 있다(ARTEL-257).
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.knowledge import (
    FORGET_KNOWLEDGE_DESCRIPTION,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_TAGS,
    LINK_KNOWLEDGE_DESCRIPTION,
    RECORD_KNOWLEDGE_DESCRIPTION,
    UNCONFIRMED_WRITE,
    UNLINK_KNOWLEDGE_DESCRIPTION,
    UPDATE_KNOWLEDGE_DESCRIPTION,
    render_entry_label,
    render_missing_knowledge_warning,
)
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import KnowledgeRequestFailed, QaCancelled, with_operator_messages
from app.qa.envelope import (
    KnowledgeCreatePayload,
    KnowledgeDeletePayload,
    KnowledgeLinkPayload,
    KnowledgeUnlinkPayload,
    KnowledgeUpdatePayload,
    MessageType,
)


def build_knowledge_write_tools(ctx: ToolContext) -> list[BaseTool]:
    # ctx 가 든 것을 여기서 되묶는다. 아래 tool 은 `build_tools` 한 함수 안에 있던 것을
    # 그대로 옮긴 것이라, 이 줄이 있어야 본문이 한 글자도 바뀌지 않는다. 읽는 쪽에는
    # 아래 tool 이 무엇을 closure 로 잡는지 먼저 말해 주는 머리말이기도 하다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch

    @tool(
        description=RECORD_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run,
            tags=", ".join(KNOWLEDGE_TAGS),
        )
    )
    async def record_knowledge(
        step: int,
        thought: str,
        tag: str,
        summary: str,
        description: str,
        scene_name: str | None = None,
        screen_id: str | None = None,
    ) -> str:
        # What the agent reads is RECORD_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason `search_knowledge` is not:
        # nothing here touches the game, so a scene view on the result would be the
        # picture the agent already has, paid for again in context (ARTEL-180).
        #
        # Every refusal below carries `render_missing_knowledge_warning`. This tool
        # is the second half of a repair as often as it is a first write, and a
        # refusal phrased only as "nothing was recorded" reads as harmless in the
        # one case where it is not.
        outstanding = state.knowledge_deleted_unreplaced

        # The cap does not bind a replacement write. It exists to stop a run
        # narrating into the knowledge base; applied to the second half of a
        # repair it would make the budget itself the thing that loses knowledge.
        #
        # Corrections count against the same allowance — see
        # `QaRunState.knowledge_writes_attempted`.
        if state.knowledge_writes_attempted >= arch.max_records_per_run and not outstanding:
            return (
                f"You have used all {arch.max_records_per_run} knowledge writes for this "
                "run, so nothing was recorded. Carry on with the run and judge the "
                "remaining steps."
            )

        topic = (tag or "").strip().upper()
        if topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as with a search's tag. Orchestration
            # rejects an unknown topic, and its rejection never comes back down
            # this socket — so a frame sent anyway would leave the run believing it
            # had written something.
            return (
                f"{tag!r} is not a knowledge topic, so nothing was recorded. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)} and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        fact = summary.strip()
        detail = description.strip()
        if not fact or not detail:
            # Same reason as the tag: Orchestration rejects a blank one on arrival
            # and says so only on its own timeline.
            return (
                "`summary` and `description` must both say something, so nothing was "
                "recorded. Write them out and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        # The anchor is whatever the agent named, and nothing else. There is no line
        # here that reads the run's current scene, and there must not be: a rule true
        # everywhere would then be filed under whichever screen the run happened to
        # be standing on, and a rule filed that way is one the run on the next screen
        # never finds.
        scene = (scene_name or "").strip() or None
        screen = (screen_id or "").strip() or None
        if scene is None and screen is not None:
            # Orchestration refuses this pair as well. Refused here first for the
            # reason the tag and the blank summary are — and this one is worth the
            # words, because the mistake has an obvious repair the agent can make.
            return (
                "`screen_id` needs the `scene_name` it belongs to, so nothing was "
                "recorded. Name the scene as well and call this again, or leave both "
                "out if this fact is true wherever the player is."
            ) + render_missing_knowledge_warning(outstanding)

        state.knowledge_records_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_CREATE,
                KnowledgeCreatePayload(
                    tag=topic,
                    summary=fact,
                    description=detail,
                    scene_name=scene,
                    screen_id=screen,
                ),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Storing knowledge is a side errand to the verdict, so a failed write
            # is reported and the run goes on. It is still stated plainly: an agent
            # told nothing would move on believing the fact was filed.
            return (
                f"The knowledge write could not be sent — {error}. Nothing was recorded."
            ) + render_missing_knowledge_warning(outstanding)

        if isinstance(answer, KnowledgeRequestFailed):
            # A refusal reaches the model since ARTEL-331/332. It used to become an
            # ERROR row on the operator's timeline and nothing else, which meant a
            # frame this side should not have sent was reported here as a success.
            # A deletion still owed is named too — this is the path that loses it.
            return (
                f"The knowledge base refused the entry — {answer.reason}. Nothing was recorded."
            ) + render_missing_knowledge_warning(outstanding)

        replaced = bool(outstanding)
        state.knowledge_deleted_unreplaced = []
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        # "Recorded" only when Orchestration said so. Silence gets the older,
        # weaker word — the frame left, and that is all this side can claim.
        lines = [
            f'Recorded under {topic}: "{fact}".'
            if answer is not None
            else f'Sent to the knowledge base, filed under {topic}: "{fact}".'
        ]
        if answer is not None and answer.knowledge_id:
            # Into `knowledge_seen`, not `knowledge_glimpsed`. That map is the
            # precondition `update_knowledge` and `forget_knowledge` rest on, and
            # it means "read in full" — which the run wrote itself certainly is.
            # Without this a run has to spend a search to correct its own entry.
            state.knowledge_seen[answer.knowledge_id] = fact
            lines.append(
                f"Its id is {answer.knowledge_id}. Use `update_knowledge` with that "
                "id if you learn this entry is wrong later in the run — you do not "
                "need to search for it first."
            )
        if replaced:
            lines.append(
                "That completes the correction — the entry you deleted has been "
                "replaced, and nothing is outstanding. Next time use "
                "`update_knowledge`: it repairs an entry in one call, and the "
                "replacement keeps the original's id."
            )
        if answer is None:
            lines.append(UNCONFIRMED_WRITE)
        lines.append(f"{remaining} knowledge write(s) left.")
        return with_operator_messages("\n\n".join(lines), messages)

    @tool(
        description=UPDATE_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def update_knowledge(
        step: int,
        thought: str,
        knowledge_id: str,
        tag: str | None = None,
        summary: str | None = None,
        description: str | None = None,
    ) -> str:
        # What the agent reads is UPDATE_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view, for the reason given on `record_knowledge` (ARTEL-180).
        # The write itself is awaited since ARTEL-332 — briefly, and the wait is
        # bounded by `KNOWLEDGE_WRITE_TIMEOUT_SECONDS` rather than the search's,
        # because no answer is a normal outcome rather than a fault.
        #
        # The budget is `max_records_per_run`, shared with `record_knowledge`
        # rather than counted apart, because both fail the run the same way — see
        # `QaRunState.knowledge_writes_attempted`. The constraint that a repair must
        # never be left half done by the budget still holds, from both ends: a
        # refused correction changes nothing, since it is one call and the entry is
        # untouched, and a delete-then-record still has its own exemption above.
        outstanding = state.knowledge_deleted_unreplaced

        def refused(reason: str) -> str:
            """A refusal, with whatever the run still owes appended to it.

            The rule `record_knowledge`'s refusals follow, and it applies here for
            a reason particular to this tool: `record_knowledge` is exempt from the
            cap while a deletion is outstanding, so a budget refusal from HERE is
            the only one a run can meet in the middle of a delete-then-record
            repair. Phrased as a bare "nothing was changed" it would read as
            harmless in exactly the state where it is not.
            """
            return reason + render_missing_knowledge_warning(outstanding)

        if state.knowledge_writes_attempted >= arch.max_records_per_run:
            return refused(
                f"You have used all {arch.max_records_per_run} knowledge writes for "
                "this run, so nothing was changed and the entry stands as it was."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The same guard `forget_knowledge` makes, for the same reason: on the
            # far side this id resolves to a real row, and nothing there can tell
            # that the agent never read it. An entry already deleted in this run is
            # gone from `knowledge_seen` too, so a correction cannot resurrect one.
            if target in state.knowledge_glimpsed:
                # Named as a neighbour line but never read in full. Said apart from
                # the case below because otherwise the agent meets a refusal it
                # cannot explain — it can see the id right there in the transcript.
                return refused(
                    f"Nothing was changed: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Search for it so you read it in full, then correct it."
                )
            return refused(
                f"Nothing was changed: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only correct "
                "what you have read. Search for it first and use the id printed "
                "with the hit."
            )

        # `None` and `""` are different requests and are kept apart all the way
        # down: an omitted field is left alone on the far side, a field sent blank
        # is rejected there. So a blank tag falls into the refusal below rather
        # than being read as "leave the topic alone" — the two spellings must not
        # quietly mean the same thing when the message here says they do not.
        topic = tag.strip().upper() if tag is not None else None
        if topic is not None and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as on a record. Orchestration rejects an
            # unknown topic and says so only on its own timeline, so a frame sent
            # anyway would leave the run believing the entry had been corrected.
            return refused(
                f"{tag!r} is not a knowledge topic, so nothing was changed. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out to keep the "
                "topic it already has."
            )

        fact = summary.strip() if summary is not None else None
        detail = description.strip() if description is not None else None
        if (summary is not None and not fact) or (description is not None and not detail):
            return refused(
                "Nothing was changed: `summary` and `description` must say something "
                "when you send them. Leave a field out entirely to keep what the "
                "entry already has, and call this again."
            )
        if topic is None and fact is None and detail is None:
            return refused(
                "Nothing was changed: a correction has to carry at least one of "
                "`tag`, `summary` or `description`. Say what the entry should now "
                "be, or use `forget_knowledge` if it should simply be gone."
            )

        state.knowledge_updates_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_UPDATE,
                KnowledgeUpdatePayload(
                    knowledge_id=target, tag=topic, summary=fact, description=detail
                ),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing left the socket, so this entry is exactly as it was. Said
            # plainly all the same: an agent told only that something failed
            # carries on believing the entry is now right. A deletion still owed
            # from earlier is named too, for the reason `refused` gives.
            return refused(
                f"The correction could not be sent — {error}. Nothing was changed "
                "and the entry is still on file exactly as it was."
            )

        if isinstance(answer, KnowledgeRequestFailed):
            return refused(
                f"The knowledge base refused the correction — {answer.reason}. Nothing "
                "was changed and the entry is still on file exactly as it was."
            )

        # Still an entry this run has read, so it stays correctable and deletable —
        # a correction is not a reason to forget having seen it. The stored summary
        # follows the correction because it is what every later label prints: left
        # alone, a `forget_knowledge` after this would name the sentence the agent
        # has just replaced.
        state.knowledge_seen[target] = (
            fact if fact is not None else state.knowledge_seen[target]
        )
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        changed = ", ".join(
            name
            for name, value in (("tag", topic), ("summary", fact), ("description", detail))
            if value is not None
        )
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again in this run."
        # Labelled with the summary the entry now has, not the one it had. Every
        # other write result echoes what was sent, and a sentence quoted right
        # after the word "Corrected" is read as the entry's current text — printing
        # the replaced one here would teach the run the correction had not landed.
        return with_operator_messages(
            f"Corrected {render_entry_label(target, state.knowledge_seen[target])}. "
            f"Sent: {changed}; the rest of the entry is left as it was. It keeps "
            "its id, so this stays readable as a repair rather than as a deletion "
            f"and a new entry.\n\n{closing} {remaining} knowledge write(s) left.",
            messages,
        )

    @tool(description=FORGET_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_forgets_per_run))
    async def forget_knowledge(step: int, thought: str, knowledge_id: str) -> str:
        # What the agent reads is FORGET_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view here either, for the reason given on `record_knowledge`.
        if state.knowledge_forgets_attempted >= arch.max_forgets_per_run:
            return (
                f"You have used all {arch.max_forgets_per_run} knowledge deletion(s) for "
                "this run, so nothing was deleted. If another entry still looks "
                "wrong, say so in `report_step` instead of deleting it."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The whole guard against deleting blind. Orchestration resolves this id
            # to a real row and has no way to know the agent never read it, so this
            # check exists here or nowhere. An id already deleted in this run is
            # gone from `knowledge_seen` too, which is what stops a second delete.
            if target in state.knowledge_glimpsed:
                return (
                    f"Nothing was deleted: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Deleting on that is exactly what this guard is for — "
                    "search for it, read it in full, and decide then."
                )
            return (
                f"Nothing was deleted: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only delete "
                "what you have read. Search for it first and use the id printed with "
                "the hit."
            )

        state.knowledge_forgets_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_DELETE, KnowledgeDeletePayload(knowledge_id=target)
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing went out, so nothing was deleted and nothing is outstanding.
            # The entry stays in `knowledge_seen`, which leaves it retryable.
            return (
                f"The deletion could not be sent — {error}. Nothing was deleted and "
                "the entry is still on file."
            )

        if isinstance(answer, KnowledgeRequestFailed):
            # Refused, so nothing was deleted — and crucially nothing is outstanding
            # either. Returning before the bookkeeping below is what keeps this out
            # of `knowledge_deleted_unreplaced`, which exists to chase a real loss.
            return (
                f"The knowledge base refused the deletion — {answer.reason}. Nothing "
                "was deleted and the entry is still on file."
            )

        # Taken out of what may be deleted and recorded as outstanding, in that
        # order, before the result is composed: from here on a `record_knowledge`
        # that fails is able to name exactly what is missing.
        label = render_entry_label(target, state.knowledge_seen.pop(target, ""))
        state.knowledge_deleted_unreplaced.append(label)
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_forgets_per_run - state.knowledge_forgets_attempted, 0)

        # Silence is treated as a deletion that probably happened: the entry leaves
        # `knowledge_seen` and joins the outstanding list above either way. The
        # cautious reading is the safe one here — a deletion wrongly believed to
        # have failed leaves the run thinking knowledge is still on file when it
        # may not be, and that is the state this tool's warning exists to prevent.
        unknown = "\n\n" + UNCONFIRMED_WRITE if answer is None else ""
        return with_operator_messages(
            f"Deleted {label}. This cannot be undone from here.{unknown}\n\n"
            "If you deleted it in order to CORRECT it, that was `update_knowledge`, "
            "and what you have now is half a repair: call `record_knowledge` NOW "
            "with the corrected version, before anything else, or this run has "
            "removed the knowledge rather than fixed it.\n\n"
            f"{remaining} deletion(s) left in this run.",
            messages,
        )

    @tool(
        description=LINK_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_links_per_run, relations=", ".join(KNOWLEDGE_RELATIONS)
        )
    )
    async def link_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
        note: str,
    ) -> str:
        # What the agent reads is LINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason the other knowledge tools
        # are not: nothing here touches the game.
        #
        # EVERY check below happens before the frame goes out. Orchestration now
        # answers a refusal (ARTEL-332), so this is no longer the only thing
        # standing between a bad frame and a false success — but it still saves a
        # round trip, and the run's clock is the reason to keep it. The two say the
        # same thing now instead of one of them saying nothing.
        if state.knowledge_links_attempted >= arch.max_links_per_run:
            return (
                f"You have used all {arch.max_links_per_run} knowledge links for this "
                "run, so nothing was linked. Spend the rest of the run judging steps."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Use one of {', '.join(KNOWLEDGE_RELATIONS)} — and if none of them "
                "fits, do not link these two at all."
            )

        reason = (note or "").strip()
        if not reason:
            # The far side stores `note` NOT NULL and would drop this frame in
            # silence. Refused here so the agent learns the link did not happen.
            return (
                "Nothing was linked: `note` is required. It is the only record of "
                "why you thought the connection was real, and of any condition it "
                "holds under."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        if source == target:
            return "Nothing was linked: an entry cannot be related to itself."
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was linked: {endpoint!r} is not an entry this run has "
                    "been shown. Search for it first and use the id printed with the "
                    "hit or with a neighbour line."
                )

        state.knowledge_links_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_LINK,
                KnowledgeLinkPayload(
                    from_knowledge_id=source,
                    to_knowledge_id=target,
                    relation=kind,
                    note=reason,
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The link could not be sent — {error}. Nothing was linked."

        if isinstance(answer, KnowledgeRequestFailed):
            # This is the refusal the local checks above were standing in for. They
            # stay: catching a bad relation here still saves a round trip, and the
            # two now say the same thing rather than one of them saying nothing.
            return f"The knowledge base refused the link — {answer.reason}. Nothing was linked."

        messages = channel.drain_operator_messages()
        remaining = arch.max_links_per_run - state.knowledge_links_attempted
        opening = "Sent" if answer is None else "Linked"
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again."
        return with_operator_messages(
            f"{opening}: {source} {kind.lower()} {target}. {closing}\n\n"
            f"{remaining} link(s) left in this run.",
            messages,
        )

    @tool(description=UNLINK_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_unlinks_per_run))
    async def unlink_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
    ) -> str:
        # What the agent reads is UNLINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Validated locally for the same reason `link_knowledge` is: a round trip
        # saved, on a run that has a clock.
        if state.knowledge_unlinks_attempted >= arch.max_unlinks_per_run:
            return (
                f"You have used all {arch.max_unlinks_per_run} knowledge unlink(s) for "
                "this run, so nothing was removed. If another link still looks wrong, "
                "say so in `report_issue` instead."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Name the relation as it was printed to you, one of "
                f"{', '.join(KNOWLEDGE_RELATIONS)}."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was removed: {endpoint!r} is not an entry this run has "
                    "been shown, so you have not seen the link you are removing."
                )

        state.knowledge_unlinks_attempted += 1
        try:
            answer = await channel.write_knowledge(
                MessageType.KNOWLEDGE_UNLINK,
                KnowledgeUnlinkPayload(
                    from_knowledge_id=source, to_knowledge_id=target, relation=kind
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The unlink could not be sent — {error}. Nothing was removed."

        if isinstance(answer, KnowledgeRequestFailed):
            # "is not linked" arrives here, and it is worth telling the model: it
            # means the relation it believed in was never there. The local checks
            # above cannot see that — they only know the endpoints were shown.
            return f"The knowledge base refused the unlink — {answer.reason}. Nothing was removed."

        messages = channel.drain_operator_messages()
        remaining = arch.max_unlinks_per_run - state.knowledge_unlinks_attempted
        opening = (
            f"Sent: removing {source} {kind.lower()} {target}."
            if answer is None
            else f"Removed: {source} {kind.lower()} {target}."
        )
        closing = UNCONFIRMED_WRITE if answer is None else "Do not send it again."
        return with_operator_messages(
            f"{opening} {closing}\n\n{remaining} unlink(s) left in this run.",
            messages,
        )

    return [
        record_knowledge,
        update_knowledge,
        forget_knowledge,
        link_knowledge,
        unlink_knowledge,
    ]
