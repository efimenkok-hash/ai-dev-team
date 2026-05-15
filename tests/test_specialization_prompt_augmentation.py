from __future__ import annotations

import pytest

from core.specialization_hints import SpecializationHint, SpecializationHints
from core.specialization_kb import (
    SpecializationKnowledgeEntry,
    default_specialization_kb,
)
from core.specialization_prompt_augmentation import (
    SpecializationPromptAugmentation,
    SpecializationPromptAugmentor,
)


def test_find_matching_hint_returns_exact_hint_for_security_agent():
    augmentor = SpecializationPromptAugmentor()
    hints = SpecializationHints(
        (
            SpecializationHint(
                specialist_role="security_agent",
                reason="Задача трогает auth и trust boundaries.",
            ),
            SpecializationHint(
                specialist_role="data_agent",
                reason="Есть migration risk.",
            ),
        )
    )

    hint = augmentor.find_matching_hint(hints, "security_agent")

    assert hint is not None
    assert hint.specialist_role == "security_agent"
    assert "trust boundaries" in hint.reason


def test_find_matching_hint_returns_none_for_no_match():
    augmentor = SpecializationPromptAugmentor()
    hints = SpecializationHints(
        (
            SpecializationHint(
                specialist_role="devops_agent",
                reason="Нужен rollback review.",
            ),
        )
    )

    assert augmentor.find_matching_hint(hints, "security_agent") is None


def test_build_augmentation_uses_matching_hint_when_present():
    augmentor = SpecializationPromptAugmentor()
    hints = SpecializationHints(
        (
            SpecializationHint(
                specialist_role="security_agent",
                reason="Таск затрагивает secrets и permission boundaries.",
            ),
        )
    )

    augmentation = augmentor.build_augmentation("security_agent", hints)

    assert augmentation.specialist_role == "security_agent"
    assert augmentation.hint is not None
    assert "permission boundaries" in augmentation.rendered_block
    assert "role: security_agent" in augmentation.rendered_block


def test_build_augmentation_falls_back_to_kb_only_when_no_hint():
    augmentor = SpecializationPromptAugmentor()

    augmentation = augmentor.build_augmentation(
        "security_agent",
        SpecializationHints.empty(),
    )

    assert augmentation.hint is None
    assert "task_specific_hint: none" in augmentation.rendered_block
    assert "domain_summary:" in augmentation.rendered_block


def test_render_block_is_deterministic_and_non_empty():
    augmentor = SpecializationPromptAugmentor()
    hints = SpecializationHints(
        (
            SpecializationHint(
                specialist_role="data_agent",
                reason="Есть риск для migrations и data shape.",
            ),
        )
    )

    block = augmentor.render_block("data_agent", hints)

    assert block == augmentor.render_block("data_agent", hints)
    assert block.strip()


@pytest.mark.parametrize("role", ("writer_agent", "ghost_agent"))
def test_build_augmentation_rejects_unknown_or_baseline_role(role: str):
    augmentor = SpecializationPromptAugmentor()

    with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
        augmentor.build_augmentation(role, SpecializationHints.empty())


def test_specialization_prompt_augmentation_rejects_mismatched_hint_role():
    entry = default_specialization_kb().for_role("security_agent")
    hint = SpecializationHint(
        specialist_role="devops_agent",
        reason="Wrong role",
    )

    with pytest.raises(
        ValueError,
        match="specialization_hint_role_mismatch:devops_agent!=security_agent",
    ):
        SpecializationPromptAugmentation(
            specialist_role="security_agent",
            hint=hint,
            knowledge_entry=entry,
            rendered_block="x",
        )


def test_specialization_prompt_augmentation_rejects_mismatched_kb_role():
    hint = SpecializationHint(
        specialist_role="security_agent",
        reason="Right role",
    )
    entry = default_specialization_kb().for_role("devops_agent")

    with pytest.raises(
        ValueError,
        match="knowledge_entry_role_mismatch:devops_agent!=security_agent",
    ):
        SpecializationPromptAugmentation(
            specialist_role="security_agent",
            hint=hint,
            knowledge_entry=entry,
            rendered_block="x",
        )


def test_rendered_block_contains_kb_sections_and_exact_hint_only():
    augmentor = SpecializationPromptAugmentor()
    hints = SpecializationHints(
        (
            SpecializationHint(
                specialist_role="security_agent",
                reason="Нужна security-проверка auth и secrets.",
            ),
            SpecializationHint(
                specialist_role="devops_agent",
                reason="Нужен deploy review.",
            ),
        )
    )

    block = augmentor.render_block("security_agent", hints)

    assert "domain_summary:" in block
    assert "focus_areas:" in block
    assert "non_goals:" in block
    assert "Нужна security-проверка auth и secrets." in block
    assert "Нужен deploy review." not in block


def test_kb_entry_validation_rejects_invalid_role():
    with pytest.raises(ValueError, match="unknown_specialist_role:writer_agent"):
        SpecializationKnowledgeEntry(
            specialist_role="writer_agent",
            domain_summary="x",
            relevant_when=("a",),
            focus_areas=("b",),
            non_goals=("c",),
        )
