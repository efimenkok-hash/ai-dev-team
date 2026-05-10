"""Tests for core.agent_personas (Step 14a: bot persona layer)."""

import pytest

from core.agent_personas import (
    DEFAULT_PERSONAS,
    VALID_SENIORITIES,
    AgentPersona,
    PersonaRegistry,
    default_registry,
)

# ---------------------------------------------------------------------------
# constants & invariants
# ---------------------------------------------------------------------------


def test_valid_seniorities_constant():
    assert "junior" in VALID_SENIORITIES
    assert "middle" in VALID_SENIORITIES
    assert "senior" in VALID_SENIORITIES
    assert "lead" in VALID_SENIORITIES
    assert len(VALID_SENIORITIES) == 4


def test_default_personas_covers_all_nine_roles():
    roles = {p.agent_role for p in DEFAULT_PERSONAS}
    expected = {
        "coordinator_agent",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
    }
    assert roles == expected


def test_default_personas_have_unique_human_names():
    names = [p.human_name for p in DEFAULT_PERSONAS]
    assert len(set(names)) == len(names)


def test_default_personas_match_expected_callsigns():
    by_role = {p.agent_role: p for p in DEFAULT_PERSONAS}
    assert by_role["coordinator_agent"].human_name == "Координатор"
    assert by_role["planning_agent"].human_name == "Планировщик"
    assert by_role["pm_agent"].human_name == "Менеджер"
    assert by_role["architect_agent"].human_name == "Архитектор"
    assert by_role["writer_agent"].human_name == "Программист"
    assert by_role["reviewer_agent"].human_name == "Ревьюер"
    assert by_role["tester_agent"].human_name == "Тестировщик"
    assert by_role["qa_agent"].human_name == "QA-инженер"
    assert by_role["fixer_agent"].human_name == "Фиксер"


def test_default_personas_callsign_equals_title():
    """Default contract: 'должность есть имя' — callsign and title coincide."""
    for p in DEFAULT_PERSONAS:
        assert p.human_name == p.title, (
            f"{p.agent_role}: {p.human_name!r} != {p.title!r}"
        )


def test_default_personas_qualified_name_collapses_when_name_equals_title():
    """When callsign and title are identical, qualified_name returns just the
    callsign — no redundant 'Архитектор (Архитектор)' parenthetical.
    """
    for p in DEFAULT_PERSONAS:
        assert p.qualified_name == p.human_name


def test_default_personas_callsign_property_matches_human_name():
    """callsign is just an alias for human_name; both must agree."""
    for p in DEFAULT_PERSONAS:
        assert p.callsign == p.human_name


def test_default_personas_all_have_emoji():
    """Every default persona must have a thematic emoji for chat formatting."""
    for p in DEFAULT_PERSONAS:
        assert p.emoji, f"{p.agent_role} has no emoji"
        assert isinstance(p.emoji, str)


def test_default_personas_emojis_are_unique():
    """Emojis should be distinguishable so users can recognise agents at a glance."""
    emojis = [p.emoji for p in DEFAULT_PERSONAS]
    assert len(set(emojis)) == len(emojis)


def test_emoji_field_optional_default_empty():
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Test",
        title="Test",
        voice_traits=("y",),
    )
    assert p.emoji == ""


def test_emoji_field_accepts_unicode():
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Test",
        title="Test",
        voice_traits=("y",),
        emoji="🧠",
    )
    assert p.emoji == "🧠"


def test_emoji_field_strips_whitespace():
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Test",
        title="Test",
        voice_traits=("y",),
        emoji="  🧠  ",
    )
    assert p.emoji == "🧠"


def test_emoji_field_rejects_non_string():
    with pytest.raises(ValueError, match="non_string_emoji"):
        AgentPersona(
            agent_role="architect_agent",
            human_name="Test",
            title="Test",
            voice_traits=("y",),
            emoji=42,  # type: ignore[arg-type]
        )


def test_default_personas_have_voice_traits():
    for p in DEFAULT_PERSONAS:
        assert len(p.voice_traits) >= 2, f"{p.agent_role} needs >=2 traits"


# ---------------------------------------------------------------------------
# AgentPersona happy-path construction
# ---------------------------------------------------------------------------


def test_construction_happy_path_cyrillic_distinct_name_and_title():
    """When callsign != title, qualified_name keeps the parenthetical."""
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Ядро",
        title="Архитектор",
        voice_traits=("системный",),
    )
    assert p.human_name == "Ядро"
    assert p.title == "Архитектор"
    assert p.seniority == "junior"
    assert p.display_name == "Ядро"
    assert p.callsign == "Ядро"
    assert p.qualified_name == "Ядро (Архитектор)"


def test_construction_happy_path_cyrillic_same_name_and_title():
    """When callsign == title, qualified_name collapses to one word."""
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Архитектор",
        title="Архитектор",
        voice_traits=("системный",),
    )
    assert p.callsign == "Архитектор"
    assert p.display_name == "Архитектор"
    assert p.qualified_name == "Архитектор"


def test_construction_happy_path_latin():
    p = AgentPersona(
        agent_role="writer_agent",
        human_name="Forge",
        title="Programmer",
        seniority="senior",
        voice_traits=("clean",),
    )
    assert p.display_name == "Forge"
    assert p.qualified_name == "Forge (Programmer)"
    assert p.seniority == "senior"


def test_construction_strips_whitespace():
    p = AgentPersona(
        agent_role="writer_agent",
        human_name="  Кузня  ",
        title=" Программист ",
        voice_traits=("чёткий",),
    )
    assert p.human_name == "Кузня"
    assert p.title == "Программист"


def test_construction_accepts_hyphenated_name():
    p = AgentPersona(
        agent_role="qa_agent",
        human_name="Альфа-Призма",
        title="QA",
        voice_traits=("строгая",),
    )
    assert p.human_name == "Альфа-Призма"


def test_construction_accepts_apostrophe_name():
    p = AgentPersona(
        agent_role="qa_agent",
        human_name="O'Connor",
        title="QA",
        voice_traits=("strict",),
    )
    assert p.human_name == "O'Connor"


# ---------------------------------------------------------------------------
# AgentPersona validation rejections
# ---------------------------------------------------------------------------


def test_construction_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown_agent_role"):
        AgentPersona(
            agent_role="ceo_agent",
            human_name="Boss",
            title="CEO",
            voice_traits=("decisive",),
        )


def test_construction_rejects_non_string_role():
    with pytest.raises(ValueError, match="unknown_agent_role"):
        AgentPersona(
            agent_role=123,  # type: ignore[arg-type]
            human_name="Pyotr",
            title="Programmer",
            voice_traits=("y",),
        )


def test_construction_rejects_non_string_human_name():
    with pytest.raises(ValueError, match="non_string_human_name"):
        AgentPersona(
            agent_role="writer_agent",
            human_name=None,  # type: ignore[arg-type]
            title="Programmer",
            voice_traits=("y",),
        )


@pytest.mark.parametrize("bad", ["", "  ", "\n\t"])
def test_construction_rejects_empty_human_name(bad):
    with pytest.raises(ValueError, match="empty_human_name"):
        AgentPersona(
            agent_role="writer_agent",
            human_name=bad,
            title="X",
            voice_traits=("y",),
        )


@pytest.mark.parametrize("bad", ["Bad@Name", "x!", "x/y", "x\\y", "x.y"])
def test_construction_rejects_invalid_human_name_chars(bad):
    with pytest.raises(ValueError, match="invalid_human_name"):
        AgentPersona(
            agent_role="writer_agent",
            human_name=bad,
            title="X",
            voice_traits=("y",),
        )


def test_construction_rejects_too_long_human_name():
    with pytest.raises(ValueError, match="invalid_human_name"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="X" * 41,
            title="X",
            voice_traits=("y",),
        )


def test_construction_rejects_non_string_title():
    with pytest.raises(ValueError, match="non_string_title"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title=None,  # type: ignore[arg-type]
            voice_traits=("y",),
        )


@pytest.mark.parametrize("bad", ["", "  "])
def test_construction_rejects_empty_title(bad):
    with pytest.raises(ValueError, match="empty_title"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title=bad,
            voice_traits=("y",),
        )


@pytest.mark.parametrize("bad", ["god", "intern", "", "Senior"])
def test_construction_rejects_invalid_seniority(bad):
    with pytest.raises(ValueError, match="invalid_seniority"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title="X",
            seniority=bad,
            voice_traits=("y",),
        )


def test_construction_rejects_list_voice_traits():
    with pytest.raises(ValueError, match="voice_traits_must_be_tuple"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title="X",
            voice_traits=["y"],  # type: ignore[arg-type]
        )


def test_construction_rejects_empty_voice_traits():
    with pytest.raises(ValueError, match="empty_voice_traits"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title="X",
            voice_traits=(),
        )


@pytest.mark.parametrize("bad", [("x", ""), ("x", "  "), ("x", None)])
def test_construction_rejects_invalid_voice_trait(bad):
    with pytest.raises(ValueError, match="empty_voice_trait"):
        AgentPersona(
            agent_role="writer_agent",
            human_name="Pyotr",
            title="X",
            voice_traits=bad,  # type: ignore[arg-type]
        )


def test_persona_is_frozen():
    p = DEFAULT_PERSONAS[0]
    with pytest.raises(Exception):
        p.human_name = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# format_signature
# ---------------------------------------------------------------------------


def test_format_signature_prepends_callsign():
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Ядро",
        title="Архитектор",
        voice_traits=("y",),
    )
    assert p.format_signature("предлагаю стек") == "Ядро: предлагаю стек"


def test_format_signature_idempotent_when_already_signed():
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Ядро",
        title="Архитектор",
        voice_traits=("y",),
    )
    out = p.format_signature("Ядро: уже подписано")
    assert out == "Ядро: уже подписано"


def test_format_signature_idempotent_with_leading_whitespace():
    p = DEFAULT_PERSONAS[0]
    sig = p.callsign + ": "
    out = p.format_signature("\n  " + sig + "тело")
    assert out == sig + "тело"


def test_format_signature_strips_leading_whitespace_when_signing():
    p = AgentPersona(
        agent_role="writer_agent",
        human_name="Кузня",
        title="Программист",
        voice_traits=("y",),
    )
    out = p.format_signature("   тело")
    assert out == "Кузня: тело"


def test_format_signature_uses_callsign_without_role_parens():
    """Sanity: signature is `<callsign>: ...`, never `<callsign> (<title>): ...`.

    Even when callsign != title (custom personas), we don't drag the role
    parenthetical into chat signatures — that's only for /agents output.
    """
    p = AgentPersona(
        agent_role="architect_agent",
        human_name="Ядро",
        title="Архитектор",
        voice_traits=("y",),
    )
    out = p.format_signature("body")
    assert out == "Ядро: body"
    assert "(" not in out
    assert ")" not in out


def test_format_signature_rejects_empty_body():
    p = DEFAULT_PERSONAS[0]
    with pytest.raises(ValueError, match="empty_body"):
        p.format_signature("   ")


def test_format_signature_rejects_non_string_body():
    p = DEFAULT_PERSONAS[0]
    with pytest.raises(ValueError, match="non_string_body"):
        p.format_signature(None)  # type: ignore[arg-type]


def test_format_signature_handles_multiline_body():
    p = AgentPersona(
        agent_role="writer_agent",
        human_name="Кузня",
        title="Программист",
        voice_traits=("y",),
    )
    out = p.format_signature("line1\nline2\nline3")
    assert out == "Кузня: line1\nline2\nline3"


def test_qualified_name_format():
    p = AgentPersona(
        agent_role="qa_agent",
        human_name="Вердикт",
        title="QA-инженер",
        voice_traits=("строгий",),
    )
    assert p.qualified_name == "Вердикт (QA-инженер)"


def test_default_personas_qualified_names_unique():
    """Sanity: all callsigns AND all qualified_names must be unique."""
    qualified = [p.qualified_name for p in DEFAULT_PERSONAS]
    assert len(set(qualified)) == len(qualified)


# ---------------------------------------------------------------------------
# PersonaRegistry
# ---------------------------------------------------------------------------


def test_default_registry_has_nine_personas():
    reg = default_registry()
    assert len(reg) == 9


def test_default_registry_covers_all_required_roles():
    reg = default_registry()
    for role in (
        "coordinator_agent",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
    ):
        assert role in reg


def test_registry_for_role_returns_correct_persona():
    reg = default_registry()
    p = reg.for_role("architect_agent")
    assert p.human_name == "Архитектор"
    assert p.title == "Архитектор"
    assert p.qualified_name == "Архитектор"


def test_registry_for_unknown_role_raises_keyerror():
    reg = default_registry()
    with pytest.raises(KeyError, match="no_persona_for"):
        reg.for_role("ceo_agent")


def test_registry_rejects_duplicate_personas():
    p = DEFAULT_PERSONAS[0]
    with pytest.raises(ValueError, match="duplicate_persona"):
        PersonaRegistry((p, p))


def test_registry_rejects_non_persona_input():
    with pytest.raises(ValueError, match="invalid_persona_type"):
        PersonaRegistry(("not a persona",))  # type: ignore[arg-type]


def test_registry_contains_check():
    reg = default_registry()
    assert "writer_agent" in reg
    assert "ceo_agent" not in reg
    assert 42 not in reg


def test_registry_all_returns_sorted_by_role():
    reg = default_registry()
    roles = [p.agent_role for p in reg.all()]
    assert roles == sorted(roles)


def test_registry_list_roles_returns_sorted():
    reg = default_registry()
    roles = reg.list_roles()
    assert roles == sorted(roles)
    assert len(roles) == 9


def test_registry_with_subset_personas():
    custom = (
        AgentPersona(
            agent_role="planning_agent",
            human_name="OnlyOne",
            title="Solo",
            voice_traits=("y",),
        ),
    )
    reg = PersonaRegistry(custom)
    assert len(reg) == 1
    assert reg.for_role("planning_agent").human_name == "OnlyOne"
    with pytest.raises(KeyError):
        reg.for_role("writer_agent")


def test_registry_with_replaced_default():
    """Replace one default persona with custom; others stay default."""
    custom_writer = AgentPersona(
        agent_role="writer_agent",
        human_name="Custom",
        title="Custom",
        voice_traits=("y",),
    )
    others = tuple(p for p in DEFAULT_PERSONAS if p.agent_role != "writer_agent")
    reg = PersonaRegistry((*others, custom_writer))
    assert len(reg) == 9
    assert reg.for_role("writer_agent").human_name == "Custom"
    assert reg.for_role("architect_agent").human_name == "Архитектор"
