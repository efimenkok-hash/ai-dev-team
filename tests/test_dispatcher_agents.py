"""Tests for core.dispatcher_agents (Step 14b-5b).

Strategy:
- DispatcherAgentConfig: frozen, validates dispatcher type.
- build_dispatcher_agent_registry_factory: validates dispatcher eagerly,
  returns a factory callable.
- factory(tier): validates TierConfig, returns dict with all 8 required keys.
- Each agent closure: calls dispatcher.dispatch with the correct agent_role,
  passes a (system, user) message pair, returns .text of LLMResponse.
- Multi-arg agents: user message contains all inputs, labelled.
- LLMDispatchError propagates unchanged through each agent.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_projection import AgentBusProjectionService, ProjectingAgentBus
from core.agent_collaboration import AgentCollaborationPolicy
from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.dispatcher_agents import (
    DispatcherAgentConfig,
    build_dispatcher_agent_registry_factory,
    build_specialist_dispatch_request,
)
from core.llm_dispatcher import (
    LLMAttempt,
    LLMDispatcher,
    LLMDispatchError,
    LLMRequest,
    LLMResponse,
)
from core.model_tier import DEFAULT_TIERS, REQUIRED_ROLES, TierConfig
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_tier() -> TierConfig:
    return TierConfig(
        name="test",
        description="test tier",
        estimated_cost_usd=0.5,
        models_per_role={role: ("model-x",) for role in REQUIRED_ROLES},
    )


def _make_dispatcher() -> LLMDispatcher:
    return LLMDispatcher(api_key="sk-test-key-1234")


def _make_response(text: str = "result") -> LLMResponse:
    return LLMResponse(
        text=text,
        model_used="model-x",
        prompt_tokens=10,
        completion_tokens=5,
        attempts=(
            LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=1),
        ),
    )


def _make_dispatch_error() -> LLMDispatchError:
    attempt = LLMAttempt(model="m", ok=False, reason="timeout", duration_ms=1)
    return LLMDispatchError("chain_exhausted", "all failed", (attempt,))


def _patched_registry(text: str = "agent-output"):
    """Return (dispatcher, registry, tier) with dispatch() stubbed to succeed."""
    d = _make_dispatcher()
    tier = _make_tier()
    resp = _make_response(text)
    factory = build_dispatcher_agent_registry_factory(d)
    registry = factory(tier)
    d.dispatch = MagicMock(return_value=resp)  # type: ignore[method-assign]
    return d, registry, tier


def _make_collaboration_bus(tmp_path):
    db = StateDB(tmp_path / "dispatcher-collaboration.db")
    registry = ProjectRegistry(db)
    registry.register_project(
        ProjectSnapshot(
            project=Project(
                project_id="alpha_project",
                slug="alpha-project",
                name="Alpha",
                description="Alpha project",
                owner_user_id=101,
                status="active",
            ),
            policy=ProjectPolicy(
                project_id="alpha_project",
                allow_hiring=True,
                allow_agent_dm=False,
                require_owner_approval_for_hires=True,
            ),
            chat_binding=ProjectChatBinding(
                project_id="alpha_project",
                chat_id=-100123,
                chat_provider="telegram",
            ),
        )
    )
    sent_envelopes = []
    backend_bus = StateBackedAgentBus(db)
    projecting_bus = ProjectingAgentBus(
        backend_bus,
        AgentBusProjectionService(
            registry,
            lambda envelope: sent_envelopes.append(envelope),
        ),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    return projecting_bus, thread, sent_envelopes


# ---------------------------------------------------------------------------
# DispatcherAgentConfig
# ---------------------------------------------------------------------------


class TestDispatcherAgentConfig:
    def test_valid_dispatcher_accepted(self):
        d = _make_dispatcher()
        cfg = DispatcherAgentConfig(dispatcher=d)
        assert cfg.dispatcher is d

    def test_frozen_prevents_reassignment(self):
        cfg = DispatcherAgentConfig(dispatcher=_make_dispatcher())
        with pytest.raises((AttributeError, TypeError)):
            cfg.dispatcher = _make_dispatcher()  # type: ignore[misc]

    def test_invalid_dispatcher_raises_value_error_none(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            DispatcherAgentConfig(dispatcher=None)  # type: ignore[arg-type]

    def test_invalid_dispatcher_raises_value_error_string(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            DispatcherAgentConfig(dispatcher="not-a-dispatcher")  # type: ignore[arg-type]

    def test_invalid_dispatcher_raises_value_error_int(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            DispatcherAgentConfig(dispatcher=42)  # type: ignore[arg-type]

    def test_invalid_dispatcher_raises_value_error_dict(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            DispatcherAgentConfig(dispatcher={})  # type: ignore[arg-type]

    def test_config_is_hashable(self):
        cfg = DispatcherAgentConfig(dispatcher=_make_dispatcher())
        assert isinstance(hash(cfg), int)

    def test_two_configs_with_same_dispatcher_equal(self):
        d = _make_dispatcher()
        cfg1 = DispatcherAgentConfig(dispatcher=d)
        cfg2 = DispatcherAgentConfig(dispatcher=d)
        assert cfg1 == cfg2

    def test_two_configs_with_different_dispatchers_not_equal(self):
        cfg1 = DispatcherAgentConfig(dispatcher=_make_dispatcher())
        cfg2 = DispatcherAgentConfig(dispatcher=_make_dispatcher())
        assert cfg1 != cfg2


# ---------------------------------------------------------------------------
# build_dispatcher_agent_registry_factory — outer function
# ---------------------------------------------------------------------------


class TestBuildDispatcherAgentRegistryFactory:
    def test_returns_callable_for_valid_dispatcher(self):
        factory = build_dispatcher_agent_registry_factory(_make_dispatcher())
        assert callable(factory)

    def test_raises_for_none_dispatcher(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            build_dispatcher_agent_registry_factory(None)  # type: ignore[arg-type]

    def test_raises_for_string_dispatcher(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            build_dispatcher_agent_registry_factory("bad")  # type: ignore[arg-type]

    def test_raises_for_int_dispatcher(self):
        with pytest.raises(ValueError, match="invalid_dispatcher_type"):
            build_dispatcher_agent_registry_factory(0)  # type: ignore[arg-type]

    def test_different_dispatchers_produce_independent_factories(self):
        f1 = build_dispatcher_agent_registry_factory(_make_dispatcher())
        f2 = build_dispatcher_agent_registry_factory(_make_dispatcher())
        assert f1 is not f2


# ---------------------------------------------------------------------------
# factory(tier) — returned callable
# ---------------------------------------------------------------------------


class TestFactory:
    @pytest.fixture
    def factory(self):
        return build_dispatcher_agent_registry_factory(_make_dispatcher())

    def test_returns_dict_for_valid_tier(self, factory):
        registry = factory(_make_tier())
        assert isinstance(registry, dict)

    def test_registry_has_all_required_agents(self, factory):
        registry = factory(_make_tier())
        for role in REQUIRED_ROLES:
            assert role in registry, f"missing: {role}"

    def test_all_registry_values_are_callable(self, factory):
        registry = factory(_make_tier())
        for name, fn in registry.items():
            assert callable(fn), f"not callable: {name}"

    def test_raises_for_none_tier(self, factory):
        with pytest.raises(ValueError, match="invalid_tier_type"):
            factory(None)

    def test_raises_for_string_tier(self, factory):
        with pytest.raises(ValueError, match="invalid_tier_type"):
            factory("standard")

    def test_raises_for_int_tier(self, factory):
        with pytest.raises(ValueError, match="invalid_tier_type"):
            factory(1)

    def test_different_tiers_produce_independent_registries(self, factory):
        r1 = factory(_make_tier())
        r2 = factory(_make_tier())
        assert r1 is not r2

    def test_same_tier_produces_fresh_registry_each_call(self, factory):
        tier = _make_tier()
        r1 = factory(tier)
        r2 = factory(tier)
        assert r1 is not r2

    def test_no_extra_keys_in_registry(self, factory):
        registry = factory(_make_tier())
        assert set(registry.keys()) == REQUIRED_ROLES


# ---------------------------------------------------------------------------
# Individual agent closures — call mechanics
# ---------------------------------------------------------------------------


class TestPlanningAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("planning-result")
        result = registry["planning_agent"]("build a CLI tool")
        assert result == "planning-result"

    def test_dispatch_called_once(self):
        d, registry, _ = _patched_registry()
        registry["planning_agent"]("task text")
        d.dispatch.assert_called_once()

    def test_agent_role_is_planning_agent(self):
        d, registry, _ = _patched_registry()
        registry["planning_agent"]("task text")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "planning_agent"

    def test_tier_passed_to_dispatch(self):
        d, registry, tier = _patched_registry()
        registry["planning_agent"]("task text")
        passed_tier = d.dispatch.call_args[0][1]
        assert passed_tier is tier

    def test_messages_contain_system_and_user(self):
        d, registry, _ = _patched_registry()
        registry["planning_agent"]("my task")
        req: LLMRequest = d.dispatch.call_args[0][0]
        roles = [m["role"] for m in req.messages]
        assert roles == ["system", "user"]

    def test_user_message_contains_task(self):
        d, registry, _ = _patched_registry()
        registry["planning_agent"]("my task")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "my task" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["planning_agent"]("task")


class TestPmAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("pm-result")
        result = registry["pm_agent"]("planning output here")
        assert result == "pm-result"

    def test_agent_role_is_pm_agent(self):
        d, registry, _ = _patched_registry()
        registry["pm_agent"]("planning output")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "pm_agent"

    def test_messages_structure(self):
        d, registry, _ = _patched_registry()
        registry["pm_agent"]("planning data")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert [m["role"] for m in req.messages] == ["system", "user"]

    def test_user_message_contains_input(self):
        d, registry, _ = _patched_registry()
        registry["pm_agent"]("my planning output")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "my planning output" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["pm_agent"]("task")


class TestArchitectAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("arch-result")
        result = registry["architect_agent"]("pm plan json")
        assert result == "arch-result"

    def test_agent_role_is_architect_agent(self):
        d, registry, _ = _patched_registry()
        registry["architect_agent"]("spec")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "architect_agent"

    def test_user_message_contains_spec(self):
        d, registry, _ = _patched_registry()
        registry["architect_agent"]("pm_plan_data")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "pm_plan_data" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["architect_agent"]("spec")


class TestWriterAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("writer-result")
        result = registry["writer_agent"]("arch plan")
        assert result == "writer-result"

    def test_agent_role_is_writer_agent(self):
        d, registry, _ = _patched_registry()
        registry["writer_agent"]("arch json")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "writer_agent"

    def test_user_message_contains_architecture(self):
        d, registry, _ = _patched_registry()
        registry["writer_agent"]("arch_data_here")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "arch_data_here" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["writer_agent"]("arch")


class TestReviewerAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("review-result")
        result = registry["reviewer_agent"]("code here", "arch here")
        assert result == "review-result"

    def test_agent_role_is_reviewer_agent(self):
        d, registry, _ = _patched_registry()
        registry["reviewer_agent"]("code", "arch")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "reviewer_agent"

    def test_user_message_contains_writer_output(self):
        d, registry, _ = _patched_registry()
        registry["reviewer_agent"]("writer_code_block", "arch_json")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "writer_code_block" in user_msg["content"]

    def test_user_message_contains_arch_plan(self):
        d, registry, _ = _patched_registry()
        registry["reviewer_agent"]("code_block", "arch_json_block")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "arch_json_block" in user_msg["content"]

    def test_user_message_labels_both_sections(self):
        d, registry, _ = _patched_registry()
        registry["reviewer_agent"]("c", "a")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "writer_output:" in user_msg["content"]
        assert "arch_plan:" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["reviewer_agent"]("code", "arch")


class TestTesterAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("test-result")
        result = registry["tester_agent"]("code here", "arch here")
        assert result == "test-result"

    def test_agent_role_is_tester_agent(self):
        d, registry, _ = _patched_registry()
        registry["tester_agent"]("code", "arch")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "tester_agent"

    def test_user_message_contains_both_inputs(self):
        d, registry, _ = _patched_registry()
        registry["tester_agent"]("code_block_xyz", "arch_block_abc")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "code_block_xyz" in user_msg["content"]
        assert "arch_block_abc" in user_msg["content"]

    def test_user_message_labels_both_sections(self):
        d, registry, _ = _patched_registry()
        registry["tester_agent"]("c", "a")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        assert "writer_output:" in user_msg["content"]
        assert "arch_plan:" in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["tester_agent"]("code", "arch")


class TestQaAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("qa-result")
        result = registry["qa_agent"]("pm", "arch", "code", "review", "tests")
        assert result == "qa-result"

    def test_agent_role_is_qa_agent(self):
        d, registry, _ = _patched_registry()
        registry["qa_agent"]("pm", "arch", "code", "review", "tests")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "qa_agent"

    def test_user_message_contains_all_five_inputs(self):
        d, registry, _ = _patched_registry()
        registry["qa_agent"]("PM_DATA", "ARCH_DATA", "CODE_DATA", "REVIEW_DATA", "TEST_DATA")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        for token in ("PM_DATA", "ARCH_DATA", "CODE_DATA", "REVIEW_DATA", "TEST_DATA"):
            assert token in user_msg["content"]

    def test_user_message_labels_all_five_sections(self):
        d, registry, _ = _patched_registry()
        registry["qa_agent"]("a", "b", "c", "d", "e")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        for label in ("pm_plan:", "arch_plan:", "writer_output:", "review:", "test_output:"):
            assert label in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["qa_agent"]("p", "a", "c", "r", "t")


class TestFixerAgent:
    def test_returns_response_text(self):
        _, registry, _ = _patched_registry("fix-result")
        result = registry["fixer_agent"]("code", "for_fixer_list", "arch")
        assert result == "fix-result"

    def test_agent_role_is_fixer_agent(self):
        d, registry, _ = _patched_registry()
        registry["fixer_agent"]("code", "for_fixer", "arch")
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == "fixer_agent"

    def test_user_message_contains_all_three_inputs(self):
        d, registry, _ = _patched_registry()
        registry["fixer_agent"]("CODE_DATA", "FIXER_DATA", "ARCH_DATA")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        for token in ("CODE_DATA", "FIXER_DATA", "ARCH_DATA"):
            assert token in user_msg["content"]

    def test_user_message_labels_all_three_sections(self):
        d, registry, _ = _patched_registry()
        registry["fixer_agent"]("c", "f", "a")
        req: LLMRequest = d.dispatch.call_args[0][0]
        user_msg = next(m for m in req.messages if m["role"] == "user")
        for label in ("writer_output:", "for_fixer:", "arch_plan:"):
            assert label in user_msg["content"]

    def test_dispatch_error_propagates(self):
        d = _make_dispatcher()
        registry = build_dispatcher_agent_registry_factory(d)(_make_tier())
        d.dispatch = MagicMock(side_effect=_make_dispatch_error())  # type: ignore[method-assign]
        with pytest.raises(LLMDispatchError):
            registry["fixer_agent"]("code", "fix", "arch")


class TestDispatcherCostEstimator:
    def test_registry_exposes_cost_estimator(self):
        _, registry, _ = _patched_registry()
        assert callable(getattr(registry, "cost_estimator", None))

    def test_cost_estimator_returns_tokens_and_cost_for_last_response(self):
        d = _make_dispatcher()
        tier = _make_tier()
        response = LLMResponse(
            text="fix-result",
            model_used="model-x",
            prompt_tokens=120,
            completion_tokens=30,
            attempts=(
                LLMAttempt(model="model-a", ok=False, reason="timeout", duration_ms=1),
                LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=2),
            ),
        )
        factory = build_dispatcher_agent_registry_factory(d)
        registry = factory(tier)
        d.dispatch = MagicMock(return_value=response)  # type: ignore[method-assign]

        output = registry["fixer_agent"]("code", "for_fixer", "arch")
        estimator = registry.cost_estimator

        in_tokens, out_tokens, cost_usd = estimator(
            "fixer_agent",
            ("code", "for_fixer", "arch"),
            output,
        )

        assert in_tokens == 120
        assert out_tokens == 30
        assert cost_usd > 0.0

    def test_cost_estimator_returns_zero_for_mismatched_call(self):
        _, registry, _ = _patched_registry("writer-result")
        registry["writer_agent"]("arch-data")

        in_tokens, out_tokens, cost_usd = registry.cost_estimator(
            "writer_agent",
            ("different-arch",),
            "writer-result",
        )

        assert (in_tokens, out_tokens, cost_usd) == (0, 0, 0.0)


# ---------------------------------------------------------------------------
# Specialist prompt contracts (G1.2 readiness without activation)
# ---------------------------------------------------------------------------


class TestSpecialistDispatchRequest:
    @pytest.mark.parametrize(
        ("role", "marker"),
        (
            ("security_agent", "hardening"),
            ("devops_agent", "deployability"),
            ("data_agent", "schema"),
        ),
    )
    def test_build_specialist_dispatch_request_happy_path(self, role, marker):
        request = build_specialist_dispatch_request(
            role,
            "Проверь specialist readiness",
        )

        assert request.agent_role == role
        assert [message["role"] for message in request.messages] == ["system", "user"]
        assert marker in request.messages[0]["content"]
        assert "Specialist expert task" in request.messages[1]["content"]
        assert "Проверь specialist readiness" in request.messages[1]["content"]

    def test_build_specialist_dispatch_request_includes_optional_context(self):
        request = build_specialist_dispatch_request(
            "security_agent",
            "Проверь security assumptions",
            context_block="project_id=alpha_project\ntask_id=task-42",
        )

        user_content = request.messages[1]["content"]
        assert "Context:" in user_content
        assert "project_id=alpha_project" in user_content
        assert "task_id=task-42" in user_content

    def test_specialist_prompts_are_distinct_and_not_json_locked(self):
        security_request = build_specialist_dispatch_request("security_agent", "A")
        devops_request = build_specialist_dispatch_request("devops_agent", "A")
        data_request = build_specialist_dispatch_request("data_agent", "A")

        security_prompt = security_request.messages[0]["content"]
        devops_prompt = devops_request.messages[0]["content"]
        data_prompt = data_request.messages[0]["content"]

        assert security_prompt != devops_prompt
        assert security_prompt != data_prompt
        assert devops_prompt != data_prompt
        assert "никакого JSON по умолчанию" in security_prompt
        assert "никакого JSON по умолчанию" in devops_prompt
        assert "никакого JSON по умолчанию" in data_prompt

    @pytest.mark.parametrize("role", ("writer_agent", "ghost_agent"))
    def test_build_specialist_dispatch_request_rejects_non_specialist_role(self, role):
        with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
            build_specialist_dispatch_request(role, "Task")

    def test_build_specialist_dispatch_request_rejects_non_string_role(self):
        with pytest.raises(ValueError, match="invalid_specialist_role_type:int"):
            build_specialist_dispatch_request(42, "Task")  # type: ignore[arg-type]

    @pytest.mark.parametrize("task_text", ("", "   "))
    def test_build_specialist_dispatch_request_rejects_empty_task(self, task_text):
        with pytest.raises(ValueError, match="empty_specialist_task_text"):
            build_specialist_dispatch_request("security_agent", task_text)

    @pytest.mark.parametrize("context_block", ("", "   "))
    def test_build_specialist_dispatch_request_rejects_empty_context_block(
        self,
        context_block,
    ):
        with pytest.raises(ValueError, match="empty_specialist_context_block"):
            build_specialist_dispatch_request(
                "data_agent",
                "Check data invariants",
                context_block=context_block,
            )


class TestSpecialistPromptNonActivation:
    def test_default_dispatcher_registry_does_not_auto_include_specialists(self):
        factory = build_dispatcher_agent_registry_factory(_make_dispatcher())
        registry = factory(_make_tier())

        assert set(registry.keys()) == REQUIRED_ROLES
        for role in SPECIALIST_ROLE_ORDER:
            assert role not in registry

    def test_specialist_dispatch_request_is_dispatchable_through_tier_dispatcher(
        self,
    ):
        dispatcher = _make_dispatcher()
        request = build_specialist_dispatch_request(
            "security_agent",
            "check threat model",
        )
        dispatcher._try_model = MagicMock(  # type: ignore[method-assign]
            return_value=("specialist-answer", 12, 7)
        )

        response = dispatcher.dispatch(request, DEFAULT_TIERS[0])

        expected_model = DEFAULT_TIERS[0].specialist_chain_for("security_agent")[0]
        dispatcher._try_model.assert_called_once_with(expected_model, request)
        assert response.text == "specialist-answer"
        assert response.model_used == expected_model


# ---------------------------------------------------------------------------
# Cross-cutting: all agents use correct tier + messages format
# ---------------------------------------------------------------------------


class TestAllAgentsMessageFormat:
    """Verify every agent in the registry sends (system, user) pair."""

    _CALLS: ClassVar[dict[str, tuple]] = {
        "planning_agent": ("task content",),
        "pm_agent": ("planning content",),
        "architect_agent": ("pm content",),
        "writer_agent": ("arch content",),
        "reviewer_agent": ("code content", "arch content"),
        "tester_agent": ("code content", "arch content"),
        "qa_agent": ("pm", "arch", "code", "review", "test"),
        "fixer_agent": ("code", "for_fixer_data", "arch"),
    }

    @pytest.fixture
    def setup(self):
        d = _make_dispatcher()
        tier = _make_tier()
        resp = _make_response("ok")
        factory = build_dispatcher_agent_registry_factory(d)
        registry = factory(tier)
        d.dispatch = MagicMock(return_value=resp)  # type: ignore[method-assign]
        return d, registry, tier

    @pytest.mark.parametrize("role", sorted(REQUIRED_ROLES))
    def test_messages_are_system_user_pair(self, setup, role):
        d, registry, _ = setup
        args = self._CALLS[role]
        registry[role](*args)
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert len(req.messages) == 2
        assert req.messages[0]["role"] == "system"
        assert req.messages[1]["role"] == "user"

    @pytest.mark.parametrize("role", sorted(REQUIRED_ROLES))
    def test_agent_role_matches_registry_key(self, setup, role):
        d, registry, _ = setup
        args = self._CALLS[role]
        registry[role](*args)
        req: LLMRequest = d.dispatch.call_args[0][0]
        assert req.agent_role == role

    @pytest.mark.parametrize("role", sorted(REQUIRED_ROLES))
    def test_tier_passed_unchanged(self, setup, role):
        d, registry, tier = setup
        args = self._CALLS[role]
        registry[role](*args)
        passed_tier = d.dispatch.call_args[0][1]
        assert passed_tier is tier

    @pytest.mark.parametrize("role", sorted(REQUIRED_ROLES))
    def test_system_prompt_is_non_empty(self, setup, role):
        d, registry, _ = setup
        args = self._CALLS[role]
        registry[role](*args)
        req: LLMRequest = d.dispatch.call_args[0][0]
        system_content = req.messages[0]["content"]
        assert len(system_content.strip()) > 50

    @pytest.mark.parametrize("role", sorted(REQUIRED_ROLES))
    def test_return_value_is_response_text(self, setup, role):
        _, registry, _ = setup
        args = self._CALLS[role]
        result = registry[role](*args)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Compatibility with orchestrator.REQUIRED_AGENTS
# ---------------------------------------------------------------------------


class TestOrchestratorCompatibility:
    def test_registry_satisfies_orchestrator_required_agents(self):
        from core.memory import PipelineMemory
        from core.orchestrator import Orchestrator

        d = _make_dispatcher()
        factory = build_dispatcher_agent_registry_factory(d)
        registry = factory(_make_tier())

        # Orchestrator.__init__ validates the registry — this must not raise.
        mem = PipelineMemory()
        orch = Orchestrator(memory=mem, agents=registry)
        assert orch is not None


class TestDispatcherAgentCollaboration:
    @pytest.mark.parametrize(
        ("role", "args", "payload"),
        (
            ("planning_agent", ("task",), '{"plan":"ok"}'),
            ("pm_agent", ("task",), '{"tasks":[]}'),
            ("architect_agent", ("task",), '{"arch":"ok"}'),
        ),
    )
    def test_final_output_compatibility_is_preserved(
        self,
        tmp_path,
        role,
        args,
        payload,
    ):
        dispatcher = _make_dispatcher()
        tier = _make_tier()
        factory = build_dispatcher_agent_registry_factory(dispatcher)
        projecting_bus, thread, _sent = _make_collaboration_bus(tmp_path)
        dispatcher.dispatch = MagicMock(return_value=_make_response(payload))  # type: ignore[method-assign]

        registry = factory.build_collaboration_registry(
            tier,
            project_id="alpha_project",
            task_id="task-42",
            thread=thread,
            owner_task_text="Owner task",
            bus=projecting_bus,
        )

        result = registry[role](*args)

        assert result == payload

    def test_one_consultation_path_returns_final_payload_and_persists_bus_roundtrip(
        self,
        tmp_path,
    ):
        dispatcher = _make_dispatcher()
        tier = _make_tier()
        factory = build_dispatcher_agent_registry_factory(dispatcher)
        projecting_bus, thread, sent_envelopes = _make_collaboration_bus(tmp_path)
        call_counts: dict[str, int] = {}

        def _dispatch(req: LLMRequest, _tier: TierConfig) -> LLMResponse:
            call_counts[req.agent_role] = call_counts.get(req.agent_role, 0) + 1
            if req.agent_role == "planning_agent" and call_counts[req.agent_role] == 1:
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ"}'
                )
            if req.agent_role == "reviewer_agent":
                assert "INTERNAL CONSULTATION MODE" in req.messages[0]["content"]
                return _make_response("Есть риск silent regression в валидации.")
            if req.agent_role == "planning_agent":
                assert "INTERNAL CONSULTATION TRANSCRIPT" in req.messages[1]["content"]
                return _make_response('{"plan":"ok"}')
            raise AssertionError(f"unexpected role {req.agent_role}")

        dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
        registry = factory.build_collaboration_registry(
            tier,
            project_id="alpha_project",
            task_id="task-42",
            thread=thread,
            owner_task_text="Owner task",
            bus=projecting_bus,
        )

        result = registry["planning_agent"]("Нужен план")

        assert result == '{"plan":"ok"}'
        messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
        assert tuple(message.message_kind for message in messages) == ("request", "reply")
        assert messages[0].sender_role == "planning_agent"
        assert messages[0].recipient_role == "reviewer_agent"
        assert messages[1].sender_role == "reviewer_agent"
        assert messages[1].in_reply_to is not None
        assert messages[1].in_reply_to.message_id == messages[0].message_id
        assert sent_envelopes[0].sender_role == "planning_agent"
        assert sent_envelopes[1].sender_role == "reviewer_agent"

    def test_repeated_consultation_beyond_limit_is_rejected(self, tmp_path):
        dispatcher = _make_dispatcher()
        tier = _make_tier()
        factory = build_dispatcher_agent_registry_factory(dispatcher)
        projecting_bus, thread, _sent = _make_collaboration_bus(tmp_path)
        call_counts: dict[str, int] = {}

        def _dispatch(req: LLMRequest, _tier: TierConfig) -> LLMResponse:
            call_counts[req.agent_role] = call_counts.get(req.agent_role, 0) + 1
            if req.agent_role == "planning_agent" and call_counts[req.agent_role] == 1:
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ"}'
                )
            if req.agent_role == "reviewer_agent":
                return _make_response("Есть риск silent regression в валидации.")
            if req.agent_role == "planning_agent":
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"tester_agent","question":"Хочу ещё одно мнение"}'
                )
            raise AssertionError(f"unexpected role {req.agent_role}")

        dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
        registry = factory.build_collaboration_registry(
            tier,
            project_id="alpha_project",
            task_id="task-42",
            thread=thread,
            owner_task_text="Owner task",
            bus=projecting_bus,
            policy=AgentCollaborationPolicy(max_consultations_per_call=1),
        )

        with pytest.raises(ValueError, match="consultation_limit_exceeded:planning_agent"):
            registry["planning_agent"]("Нужен план")

    def test_consultation_dispatch_error_propagates_without_fake_reply(
        self,
        tmp_path,
    ):
        dispatcher = _make_dispatcher()
        tier = _make_tier()
        factory = build_dispatcher_agent_registry_factory(dispatcher)
        projecting_bus, thread, _sent = _make_collaboration_bus(tmp_path)
        dispatch_error = _make_dispatch_error()
        call_counts: dict[str, int] = {}

        def _dispatch(req: LLMRequest, _tier: TierConfig) -> LLMResponse:
            call_counts[req.agent_role] = call_counts.get(req.agent_role, 0) + 1
            if req.agent_role == "planning_agent":
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ"}'
                )
            raise dispatch_error

        dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
        registry = factory.build_collaboration_registry(
            tier,
            project_id="alpha_project",
            task_id="task-42",
            thread=thread,
            owner_task_text="Owner task",
            bus=projecting_bus,
        )

        with pytest.raises(LLMDispatchError):
            registry["planning_agent"]("Нужен план")

        messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
        assert tuple(message.message_kind for message in messages) == ("request",)
