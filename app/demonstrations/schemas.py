import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


Text = Annotated[str, Field(min_length=1, max_length=10000)]
EventId = Annotated[int, Field(gt=0, strict=True)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcedureStep(ContractModel):
    instruction: Text
    target: str = Field(max_length=10000)
    completionCondition: Text
    evidenceEventIds: list[EventId] = Field(min_length=1, max_length=100)


class ProcedureAssertion(ContractModel):
    expected: Text
    failure: Text
    when: Text
    evidenceEventIds: list[EventId] = Field(min_length=1, max_length=100)


class VerificationProcedure(ContractModel):
    title: str = Field(min_length=1, max_length=500)
    preconditions: list[Text]
    steps: list[ProcedureStep] = Field(max_length=100)
    assertions: list[ProcedureAssertion] = Field(max_length=50)
    observedOutcome: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    facts: list[Text]
    inferences: list[Text]
    unresolvedQuestions: list[Text]

    @model_validator(mode="after")
    def incomplete_procedure_requires_question(self):
        if (not self.steps or not self.assertions) and not self.unresolvedQuestions:
            raise ValueError("Incomplete procedure requires unresolvedQuestions")
        return self


class ApprovedProcedure(ContractModel):
    id: EventId
    revision: EventId
    source_hash: Text
    procedure: VerificationProcedure

    @model_validator(mode="after")
    def approved_has_no_questions(self):
        if self.procedure.unresolvedQuestions:
            raise ValueError("Approved procedure cannot contain unresolvedQuestions")
        return self


class DemonstrationCase(ContractModel):
    id: EventId
    scene: str | None = None
    precondition: str | None = None
    test_step: str | None = None
    expected: str


class DemonstrationEvent(ContractModel):
    id: EventId
    kind: Literal["INPUT", "ACTION_RESULT", "PULSE", "EVIDENCE", "ANNOTATION", "CONTROL"]
    payloadJson: str = Field(max_length=2000000)

    @field_validator("payloadJson")
    @classmethod
    def valid_json(cls, value: str) -> str:
        json.loads(value)
        return value


class DemonstrationImage(ContractModel):
    eventId: EventId
    url: HttpUrl


class ExtractionRequest(ContractModel):
    demonstrationId: Text
    sourceHash: Text
    case: DemonstrationCase
    events: list[DemonstrationEvent] = Field(min_length=1, max_length=10000)
    images: list[DemonstrationImage] = Field(max_length=100)

    @model_validator(mode="after")
    def references_exist(self):
        event_ids = {event.id for event in self.events}
        if len(event_ids) != len(self.events):
            raise ValueError("Duplicate event IDs")
        evidence_ids = {event.id for event in self.events if event.kind == "EVIDENCE"}
        if any(image.eventId not in evidence_ids for image in self.images):
            raise ValueError("Image must reference an EVIDENCE event")
        if sum(len(event.payloadJson) for event in self.events) > 2000000:
            raise ValueError("Demonstration source exceeds 2 million characters")
        return self


class ExtractionResponse(ContractModel):
    sourceHash: str
    procedure: VerificationProcedure
