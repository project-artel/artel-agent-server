"""실행 설정 중 Home에 표시할 필드와 조회 응답."""

from dataclasses import dataclass
from functools import cache
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class UnavailableWhen(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    equals: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class LaunchOption:
    """label code는 같은 선택이 기존 run과 같은 label을 만들도록 유지한다."""

    label_code: str
    ko: str
    en: str
    engines: tuple[str, ...] = ("agent",)
    vision_only_values: tuple[str, ...] = ()
    unavailable_when: tuple[UnavailableWhen, ...] = ()


class OptionDescription(BaseModel):
    ko: str
    en: str


class ArchOption(BaseModel):
    key: str
    label_code: str = Field(serialization_alias="labelCode")
    description: OptionDescription
    engines: tuple[str, ...]


class ToggleOption(ArchOption):
    kind: Literal["toggle"]


class ChoiceOption(ArchOption):
    kind: Literal["choice"]
    values: list[str]
    vision_only_values: tuple[str, ...] | None = Field(default=None, serialization_alias="visionOnlyValues")
    unavailable_when: tuple[UnavailableWhen, ...] | None = Field(default=None, serialization_alias="unavailableWhen")


class NumberOption(ArchOption):
    kind: Literal["number"]
    min: int
    max: int


class ArchOptions(BaseModel):
    options: list[Annotated[ToggleOption | ChoiceOption | NumberOption, Field(discriminator="kind")]]


@cache
def launch_options(spec: type[BaseModel]) -> ArchOptions:
    schema = spec.model_json_schema(mode="validation")
    options = []
    for key, field in spec.model_fields.items():
        presentation = next((item for item in field.metadata if isinstance(item, LaunchOption)), None)
        if presentation is None:
            continue

        value_schema = schema["properties"][key]
        if "anyOf" in value_schema:
            # 서버 기본값은 키 생략이다. 배포 기본값을 따르는 nullable 필드도 편집할 값은 하나다.
            value_schema, = (item for item in value_schema["anyOf"] if item.get("type") != "null")
        if "$ref" in value_schema:
            definition = value_schema["$ref"].removeprefix("#/$defs/")
            value_schema = schema["$defs"][definition]

        common = dict(
            key=key,
            label_code=presentation.label_code,
            description=OptionDescription(ko=presentation.ko, en=presentation.en),
            engines=presentation.engines,
        )
        if value_schema["type"] == "boolean":
            option = ToggleOption(kind="toggle", **common)
        elif value_schema["type"] == "string" and "enum" in value_schema:
            option = ChoiceOption(
                kind="choice", values=value_schema["enum"],
                vision_only_values=presentation.vision_only_values or None,
                unavailable_when=presentation.unavailable_when or None, **common,
            )
        elif value_schema["type"] == "integer":
            option = NumberOption(kind="number", min=value_schema["minimum"], max=value_schema["maximum"], **common)
        else:
            raise ValueError(f"Unsupported QA launch option schema for {key}: {value_schema}")
        options.append(option)
    return ArchOptions(options=sorted(options, key=lambda option: option.label_code))
