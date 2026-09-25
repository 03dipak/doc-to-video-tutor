"""Strict Pydantic schema for lesson plans and TTS clips.

Phase 1 of the deterministic pipeline: enforce layout constraints at
generation time (max 4 bullets per scene, normalized narration text,
English-only slide fields) before rendering or audio synthesis.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .text import _has_non_latin_script

_FIELD_NAMES = ("title", "bullets", "steps", "flow", "visual_diagram",
                "code_snippet", "code_context", "analogy", "design_decision",
                "section", "topic")


class SlideScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    narration: str
    bullets: list[str] = Field(default_factory=list, max_length=4)
    steps: list[str] = Field(default_factory=list)
    flow: list[str] = Field(default_factory=list)
    visual_diagram: str = ""
    code_snippet: str = ""
    code_context: str = ""
    analogy: str = ""
    design_decision: str = ""
    section: str = ""
    topic: str = ""
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("bullets", "steps", "flow", mode="before")
    @classmethod
    def normalize_sequence_fields(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, tuple):
            return list(v)
        return v

    @field_validator("bullets")
    @classmethod
    def validate_bullets_max(cls, v: list[str]) -> list[str]:
        if len(v) > 4:
            raise ValueError("bullets per slide must be at most 4")
        for item in v:
            if _has_non_latin_script(item):
                raise ValueError("slide text must stay English (Latin script)")
        return v

    @field_validator("title")
    @classmethod
    def require_non_empty_title(cls, v: str) -> str:
        if isinstance(v, str) and not v.strip():
            raise ValueError("title must not be empty")
        return v

    @field_validator(*_FIELD_NAMES)
    @classmethod
    def reject_non_latin_slide_text(cls, v: str | list[str]) -> str | list[str]:
        values = v if isinstance(v, list) else [v]
        if any(_has_non_latin_script(str(item)) for item in values):
            raise ValueError("slide text must stay English (Latin script)")
        return v

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, v: list[str]) -> list[str]:
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("narration")
    @classmethod
    def normalize_narration(cls, v: str) -> str:
        v = re.sub(r"(\w):(\w)", r"\1: \2", v)
        v = v.replace(".jso ", ".json ")
        v = v.replace(".jso", ".json")
        return v

    @field_validator("code_context")
    @classmethod
    def normalize_code_context(cls, v: str) -> str:
        v = re.sub(r"Design choice \w+:", "", v)
        v = re.sub(r"Faisla hua ki:", "", v)
        v = re.sub(r"Yeh .*? सोच के बनाया:", "", v)
        v = re.sub(r"Iska ahem reason:", "", v)
        return v.strip()


class LessonPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    opening: str
    scenes: list[SlideScene] = Field(min_length=1)
    takeaways: list[str] = Field(default_factory=list)

    @field_validator("title", "opening")
    @classmethod
    def reject_non_latin_plan_text(cls, v: str) -> str:
        if _has_non_latin_script(v):
            raise ValueError("plan text must stay English (Latin script)")
        return v

    @field_validator("scenes")
    @classmethod
    def validate_scene_count(cls, v: list[SlideScene]) -> list[SlideScene]:
        if len(v) < 1:
            raise ValueError("at least one scene is required")
        return v

    @field_validator("takeaways")
    @classmethod
    def validate_takeaways(cls, v: list[str]) -> list[str]:
        return [str(x).strip() for x in v if str(x).strip()][:8]


class TTSClip(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    index: int
    title: str
    spoken: str
    word_count: int
    narration: str = ""

    @field_validator("spoken")
    @classmethod
    def normalize_spoken(cls, v: str) -> str:
        v = re.sub(r"(\w):(\w)", r"\1: \2", v)
        v = v.replace(".jso ", ".json ")
        v = v.replace(".jso", ".json")
        return v
