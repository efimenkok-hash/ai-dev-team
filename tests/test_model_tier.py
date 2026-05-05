"""Tests for core.model_tier (Step 14b-1: configurable model stacks)."""

import pytest

from core.model_tier import (
    DEFAULT_TIERS,
    REQUIRED_ROLES,
    ModelTierName,
    TierConfig,
    TierRegistry,
    default_registry,
    load_tiers_from_iterable,
)


def _full_chain():
    """Helper: produce a minimal valid models_per_role for tests."""
    return {role: ("model-a", "model-b") for role in REQUIRED_ROLES}


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_required_roles_covers_all_pipeline_agents():
    expected = {
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent", "fixer_agent",
    }
    assert REQUIRED_ROLES == expected


def test_model_tier_name_enum_values():
    assert ModelTierName.ECONOMY.value == "ECONOMY"
    assert ModelTierName.STANDARD.value == "STANDARD"
    assert ModelTierName.PREMIUM.value == "PREMIUM"


def test_default_tiers_has_three_entries():
    names = {t.name for t in DEFAULT_TIERS}
    assert names == {"ECONOMY", "STANDARD", "PREMIUM"}


def test_default_tiers_costs_increase():
    by_name = {t.name: t for t in DEFAULT_TIERS}
    assert by_name["ECONOMY"].estimated_cost_usd < by_name["STANDARD"].estimated_cost_usd
    assert by_name["STANDARD"].estimated_cost_usd < by_name["PREMIUM"].estimated_cost_usd


def test_default_tiers_descriptions_have_emoji():
    """Each default tier description should start with an emoji marker."""
    by_name = {t.name: t for t in DEFAULT_TIERS}
    assert "💰" in by_name["ECONOMY"].description
    assert "🛠" in by_name["STANDARD"].description
    assert "💎" in by_name["PREMIUM"].description


def test_default_tiers_cover_all_required_roles():
    for tier in DEFAULT_TIERS:
        assert set(tier.models_per_role.keys()) == REQUIRED_ROLES


def test_default_tiers_chains_non_empty():
    for tier in DEFAULT_TIERS:
        for role, chain in tier.models_per_role.items():
            assert len(chain) >= 1, f"{tier.name}/{role} chain is empty"


# ---------------------------------------------------------------------------
# TierConfig validation
# ---------------------------------------------------------------------------


def test_tier_config_happy_path():
    t = TierConfig(
        name="test",
        description="desc",
        estimated_cost_usd=1.5,
        models_per_role=_full_chain(),
    )
    assert t.name == "test"
    assert t.estimated_cost_usd == 1.5


def test_tier_config_strips_name_and_description():
    t = TierConfig(
        name="  test  ",
        description=" desc ",
        estimated_cost_usd=1.0,
        models_per_role=_full_chain(),
    )
    assert t.name == "test"
    assert t.description == "desc"


def test_tier_config_is_frozen():
    t = DEFAULT_TIERS[0]
    with pytest.raises(Exception):
        t.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_tier_config_rejects_empty_name(bad):
    with pytest.raises(ValueError, match="empty_tier_name"):
        TierConfig(
            name=bad,  # type: ignore[arg-type]
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=_full_chain(),
        )


def test_tier_config_rejects_empty_description():
    with pytest.raises(ValueError, match="empty_tier_description"):
        TierConfig(
            name="t",
            description="",
            estimated_cost_usd=1.0,
            models_per_role=_full_chain(),
        )


@pytest.mark.parametrize("bad", [0, -0.1, -1])
def test_tier_config_rejects_non_positive_cost(bad):
    with pytest.raises(ValueError, match="non_positive_cost"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=bad,
            models_per_role=_full_chain(),
        )


def test_tier_config_rejects_too_high_cost():
    with pytest.raises(ValueError, match="cost_too_high"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1000.0,
            models_per_role=_full_chain(),
        )


def test_tier_config_rejects_bool_cost():
    with pytest.raises(ValueError, match="invalid_cost_type"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=True,  # type: ignore[arg-type]
            models_per_role=_full_chain(),
        )


def test_tier_config_rejects_non_numeric_cost():
    with pytest.raises(ValueError, match="invalid_cost_type"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd="cheap",  # type: ignore[arg-type]
            models_per_role=_full_chain(),
        )


def test_tier_config_rejects_non_mapping_models():
    with pytest.raises(ValueError, match="models_per_role_must_be_mapping"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=[("planning_agent", ("m",))],  # type: ignore[arg-type]
        )


def test_tier_config_rejects_missing_role():
    incomplete = {role: ("m",) for role in REQUIRED_ROLES if role != "qa_agent"}
    with pytest.raises(ValueError, match="missing_roles:qa_agent"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=incomplete,
        )


def test_tier_config_rejects_unknown_role():
    extra = dict(_full_chain())
    extra["ceo_agent"] = ("m",)
    with pytest.raises(ValueError, match="unknown_roles:ceo_agent"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=extra,
        )


def test_tier_config_rejects_non_tuple_chain():
    bad = dict(_full_chain())
    bad["planning_agent"] = ["m"]  # type: ignore[assignment]
    with pytest.raises(ValueError, match="chain_must_be_tuple:planning_agent"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=bad,
        )


def test_tier_config_rejects_empty_chain():
    bad = dict(_full_chain())
    bad["planning_agent"] = ()
    with pytest.raises(ValueError, match="empty_chain:planning_agent"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=bad,
        )


@pytest.mark.parametrize("invalid_id", ["", "  ", None, 42])
def test_tier_config_rejects_invalid_model_id(invalid_id):
    bad = dict(_full_chain())
    bad["planning_agent"] = (invalid_id,)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=bad,
        )


def test_tier_config_rejects_duplicate_in_chain():
    bad = dict(_full_chain())
    bad["planning_agent"] = ("model-a", "model-a")
    with pytest.raises(ValueError, match="duplicate_in_chain"):
        TierConfig(
            name="t",
            description="d",
            estimated_cost_usd=1.0,
            models_per_role=bad,
        )


def test_tier_config_normalises_model_ids():
    chain = dict(_full_chain())
    chain["planning_agent"] = ("  model-a  ", "model-b")
    t = TierConfig(
        name="t",
        description="d",
        estimated_cost_usd=1.0,
        models_per_role=chain,
    )
    assert t.chain_for("planning_agent") == ("model-a", "model-b")


# ---------------------------------------------------------------------------
# TierConfig accessors
# ---------------------------------------------------------------------------


def test_chain_for_returns_correct_chain():
    t = DEFAULT_TIERS[1]  # STANDARD
    chain = t.chain_for("architect_agent")
    assert isinstance(chain, tuple)
    assert len(chain) >= 1


def test_chain_for_unknown_role_raises():
    t = DEFAULT_TIERS[0]
    with pytest.raises(KeyError, match="unknown_role"):
        t.chain_for("ceo_agent")


def test_primary_model_returns_first_in_chain():
    t = TierConfig(
        name="t",
        description="d",
        estimated_cost_usd=1.0,
        models_per_role={
            **{role: ("default-m",) for role in REQUIRED_ROLES},
            "architect_agent": ("primary", "fallback1", "fallback2"),
        },
    )
    assert t.primary_model("architect_agent") == "primary"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_to_dict_round_trip():
    original = DEFAULT_TIERS[1]
    dumped = original.to_dict()
    restored = TierConfig.from_dict(dumped)
    assert restored.name == original.name
    assert restored.description == original.description
    assert restored.estimated_cost_usd == original.estimated_cost_usd
    assert restored.models_per_role == original.models_per_role


def test_to_dict_includes_schema_version():
    d = DEFAULT_TIERS[0].to_dict()
    assert d["schema_version"] == 1


def test_from_dict_rejects_wrong_schema():
    d = DEFAULT_TIERS[0].to_dict()
    d["schema_version"] = 99
    with pytest.raises(ValueError, match="unsupported_schema_version"):
        TierConfig.from_dict(d)


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="invalid_dump_type"):
        TierConfig.from_dict("not a dict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing", ["name", "description", "estimated_cost_usd", "models_per_role"],
)
def test_from_dict_rejects_missing_required_key(missing):
    d = DEFAULT_TIERS[0].to_dict()
    del d[missing]
    with pytest.raises(ValueError, match=f"missing_key:{missing}"):
        TierConfig.from_dict(d)


# ---------------------------------------------------------------------------
# TierRegistry
# ---------------------------------------------------------------------------


def test_default_registry_has_three_tiers():
    reg = default_registry()
    assert len(reg) == 3
    assert "ECONOMY" in reg
    assert "STANDARD" in reg
    assert "PREMIUM" in reg


def test_default_registry_active_is_standard():
    reg = default_registry()
    assert reg.active_name() == "STANDARD"
    assert reg.active().name == "STANDARD"


def test_registry_set_active_changes_selection():
    reg = default_registry()
    reg.set_active("PREMIUM")
    assert reg.active_name() == "PREMIUM"
    assert reg.active().name == "PREMIUM"


def test_registry_set_active_unknown_raises():
    reg = default_registry()
    with pytest.raises(KeyError, match="unknown_tier"):
        reg.set_active("DELUXE")


def test_registry_get_returns_tier():
    reg = default_registry()
    t = reg.get("ECONOMY")
    assert t.name == "ECONOMY"


def test_registry_get_unknown_raises():
    reg = default_registry()
    with pytest.raises(KeyError, match="unknown_tier"):
        reg.get("DELUXE")


def test_registry_register_adds_new_tier():
    reg = default_registry()
    custom = TierConfig(
        name="CUSTOM",
        description="custom desc",
        estimated_cost_usd=2.5,
        models_per_role=_full_chain(),
    )
    reg.register(custom)
    assert "CUSTOM" in reg
    assert reg.get("CUSTOM").name == "CUSTOM"


def test_registry_register_duplicate_raises():
    reg = default_registry()
    duplicate = TierConfig(
        name="ECONOMY",
        description="d",
        estimated_cost_usd=1.0,
        models_per_role=_full_chain(),
    )
    with pytest.raises(ValueError, match="duplicate_tier"):
        reg.register(duplicate)


def test_registry_replace_overwrites_existing():
    reg = default_registry()
    new_economy = TierConfig(
        name="ECONOMY",
        description="updated economy",
        estimated_cost_usd=0.10,
        models_per_role=_full_chain(),
    )
    reg.replace(new_economy)
    assert reg.get("ECONOMY").description == "updated economy"


def test_registry_replace_unknown_raises():
    reg = default_registry()
    custom = TierConfig(
        name="UNKNOWN",
        description="d",
        estimated_cost_usd=1.0,
        models_per_role=_full_chain(),
    )
    with pytest.raises(KeyError, match="unknown_tier"):
        reg.replace(custom)


def test_registry_rejects_non_tier_in_constructor():
    with pytest.raises(ValueError, match="invalid_tier_type"):
        TierRegistry(["not a tier"])  # type: ignore[arg-type]


def test_registry_rejects_empty_construction():
    with pytest.raises(ValueError, match="empty_tier_registry"):
        TierRegistry([])


def test_registry_rejects_unknown_active_name():
    with pytest.raises(ValueError, match="unknown_active_tier"):
        TierRegistry(DEFAULT_TIERS, active_name="UNKNOWN")


def test_registry_list_names_sorted():
    reg = default_registry()
    assert reg.list_names() == ["ECONOMY", "PREMIUM", "STANDARD"]


def test_registry_all_returns_sorted():
    reg = default_registry()
    names = [t.name for t in reg.all()]
    assert names == sorted(names)


def test_load_tiers_from_iterable_round_trip():
    dumps = [t.to_dict() for t in DEFAULT_TIERS]
    reg = load_tiers_from_iterable(dumps)
    assert len(reg) == 3
    assert reg.active_name() == "STANDARD"


def test_load_tiers_from_iterable_custom_active():
    dumps = [t.to_dict() for t in DEFAULT_TIERS]
    reg = load_tiers_from_iterable(dumps, active_name="PREMIUM")
    assert reg.active_name() == "PREMIUM"
