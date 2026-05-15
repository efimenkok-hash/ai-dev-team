from __future__ import annotations

from dataclasses import dataclass

from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.specialization_hints import SpecializationHint, SpecializationHints
from core.specialization_kb import (
    SpecializationKnowledgeBase,
    SpecializationKnowledgeEntry,
    default_specialization_kb,
)

_SPECIALIST_ORDER_INDEX = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}


def _normalize_specialist_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("empty_specialist_role")
    normalized = role.strip().lower()
    if normalized not in _SPECIALIST_ORDER_INDEX:
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


def _normalize_hints(
    hints: SpecializationHints | None,
) -> SpecializationHints:
    if hints is None:
        return SpecializationHints.empty()
    if not isinstance(hints, SpecializationHints):
        raise ValueError(
            "invalid_specialization_hints_type:"
            f"{type(hints).__name__}"
        )
    return hints


def _render_text_list(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"- {item}" for item in items)


@dataclass(frozen=True)
class SpecializationPromptAugmentation:
    specialist_role: str
    hint: SpecializationHint | None
    knowledge_entry: SpecializationKnowledgeEntry
    rendered_block: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specialist_role",
            _normalize_specialist_role(self.specialist_role),
        )
        if not isinstance(self.knowledge_entry, SpecializationKnowledgeEntry):
            raise ValueError(
                "invalid_specialization_knowledge_entry_type:"
                f"{type(self.knowledge_entry).__name__}"
            )
        if self.knowledge_entry.specialist_role != self.specialist_role:
            raise ValueError(
                "knowledge_entry_role_mismatch:"
                f"{self.knowledge_entry.specialist_role}!={self.specialist_role}"
            )
        if self.hint is not None:
            if not isinstance(self.hint, SpecializationHint):
                raise ValueError(
                    "invalid_specialization_hint_type:"
                    f"{type(self.hint).__name__}"
                )
            if self.hint.specialist_role != self.specialist_role:
                raise ValueError(
                    "specialization_hint_role_mismatch:"
                    f"{self.hint.specialist_role}!={self.specialist_role}"
                )
        if not isinstance(self.rendered_block, str) or not self.rendered_block.strip():
            raise ValueError("empty_rendered_block")
        object.__setattr__(self, "rendered_block", self.rendered_block.strip())


class SpecializationPromptAugmentor:
    def __init__(
        self,
        knowledge_base: SpecializationKnowledgeBase | None = None,
    ) -> None:
        if knowledge_base is not None and not isinstance(
            knowledge_base, SpecializationKnowledgeBase
        ):
            raise ValueError(
                "invalid_specialization_knowledge_base_type:"
                f"{type(knowledge_base).__name__}"
            )
        self._knowledge_base = (
            knowledge_base
            if knowledge_base is not None
            else default_specialization_kb()
        )

    def find_matching_hint(
        self,
        hints: SpecializationHints,
        specialist_role: str,
    ) -> SpecializationHint | None:
        normalized_role = _normalize_specialist_role(specialist_role)
        normalized_hints = _normalize_hints(hints)
        for item in normalized_hints.items:
            if item.specialist_role == normalized_role:
                return item
        return None

    def build_augmentation(
        self,
        specialist_role: str,
        hints: SpecializationHints | None = None,
    ) -> SpecializationPromptAugmentation:
        normalized_role = _normalize_specialist_role(specialist_role)
        normalized_hints = _normalize_hints(hints)
        knowledge_entry = self._knowledge_base.for_role(normalized_role)
        hint = self.find_matching_hint(normalized_hints, normalized_role)
        rendered_block = self._render_block(
            specialist_role=normalized_role,
            knowledge_entry=knowledge_entry,
            hint=hint,
        )
        return SpecializationPromptAugmentation(
            specialist_role=normalized_role,
            hint=hint,
            knowledge_entry=knowledge_entry,
            rendered_block=rendered_block,
        )

    def render_block(
        self,
        specialist_role: str,
        hints: SpecializationHints | None = None,
    ) -> str:
        return self.build_augmentation(
            specialist_role,
            hints,
        ).rendered_block

    @staticmethod
    def _render_block(
        *,
        specialist_role: str,
        knowledge_entry: SpecializationKnowledgeEntry,
        hint: SpecializationHint | None,
    ) -> str:
        task_specific_hint = hint.reason if hint is not None else "none"
        lines = [
            "Specialization context",
            f"role: {specialist_role}",
            f"domain_summary: {knowledge_entry.domain_summary}",
            f"task_specific_hint: {task_specific_hint}",
            "",
            "relevant_when:",
            *_render_text_list(knowledge_entry.relevant_when),
            "",
            "focus_areas:",
            *_render_text_list(knowledge_entry.focus_areas),
            "",
            "non_goals:",
            *_render_text_list(knowledge_entry.non_goals),
        ]
        return "\n".join(lines)
