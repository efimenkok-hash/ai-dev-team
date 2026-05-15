from __future__ import annotations

from dataclasses import dataclass, field

from core.agent_personas import AgentPersona, PersonaRegistry
from core.agent_role_catalog import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.coordinator_onboarding import describe_context_source
from core.coordinator_role import COORDINATOR_ROLE
from core.project_registry import ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.specialization_hints import SpecializationHints

_VALID_TEAM_ASSEMBLY_CONTEXT_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)
_VALID_ASSEMBLY_MODES = frozenset({"baseline_internal_team"})

_ROLE_MANDATES = {
    COORDINATOR_ROLE: (
        "Control-plane lead and project captain; keeps project scope, "
        "context, and coordination authoritative."
    ),
    "planning_agent": (
        "Structures the task, clarifies constraints, and defines the initial "
        "execution outline."
    ),
    "pm_agent": (
        "Turns the plan into concrete internal work packages and keeps the "
        "execution sequence organized."
    ),
    "architect_agent": (
        "Defines the solution shape, tradeoffs, and implementation boundaries."
    ),
    "writer_agent": (
        "Implements the code changes inside the approved project contour."
    ),
    "reviewer_agent": (
        "Reviews the implementation for correctness, regressions, and risk."
    ),
    "tester_agent": (
        "Adds or validates tests and checks edge cases for the implementation."
    ),
    "qa_agent": (
        "Acts as the final quality gate before success is accepted."
    ),
    "fixer_agent": (
        "Applies targeted fixes when review or QA rejects the current result."
    ),
}


def _normalize_owner_task_text(owner_task_text: str) -> str:
    if not isinstance(owner_task_text, str) or not owner_task_text.strip():
        raise ValueError("empty_owner_task_text")
    return owner_task_text.strip()


def _validate_required_roles(personas: PersonaRegistry) -> None:
    missing = [
        role for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER if role not in personas
    ]
    if missing:
        raise ValueError(f"missing_required_persona_roles:{','.join(missing)}")


@dataclass(frozen=True)
class CoordinatorTeamAssemblyContext:
    snapshot: ProjectSnapshot
    owner_task_text: str
    context_source: str
    personas: PersonaRegistry
    project_specialist_roster: ProjectSpecialistRoster | None = None
    specialization_hints: SpecializationHints = field(
        default_factory=SpecializationHints.empty
    )

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_owner_task_text(self.owner_task_text),
        )
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in _VALID_TEAM_ASSEMBLY_CONTEXT_SOURCES
        ):
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        if not isinstance(self.personas, PersonaRegistry):
            raise ValueError(
                f"invalid_persona_registry_type:{type(self.personas).__name__}"
            )
        if self.project_specialist_roster is None:
            object.__setattr__(
                self,
                "project_specialist_roster",
                ProjectSpecialistRoster(
                    project_id=self.snapshot.project.project_id,
                    specialist_roles=(),
                ),
            )
        elif not isinstance(self.project_specialist_roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_project_specialist_roster_type:"
                f"{type(self.project_specialist_roster).__name__}"
            )
        elif (
            self.project_specialist_roster.project_id
            != self.snapshot.project.project_id
        ):
            raise ValueError(
                "project_specialist_roster_project_id_mismatch:"
                f"{self.project_specialist_roster.project_id}!="
                f"{self.snapshot.project.project_id}"
            )
        if not isinstance(self.specialization_hints, SpecializationHints):
            raise ValueError(
                "invalid_specialization_hints_type:"
                f"{type(self.specialization_hints).__name__}"
            )
        _validate_required_roles(self.personas)


@dataclass(frozen=True)
class AssembledTeamMember:
    persona: AgentPersona
    mandate: str
    is_captain: bool = False
    is_internal: bool = True
    is_active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.persona, AgentPersona):
            raise ValueError(
                f"invalid_agent_persona_type:{type(self.persona).__name__}"
            )
        if not isinstance(self.mandate, str) or not self.mandate.strip():
            raise ValueError("empty_member_mandate")
        object.__setattr__(self, "mandate", self.mandate.strip())
        for field_name in ("is_captain", "is_internal", "is_active"):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise ValueError(
                    f"invalid_{field_name}_type:{type(value).__name__}"
                )
        if self.is_captain and self.persona.agent_role != COORDINATOR_ROLE:
            raise ValueError("captain_must_be_coordinator")
        if not self.is_internal:
            raise ValueError("baseline_member_must_be_internal")
        if not self.is_active:
            raise ValueError("baseline_member_must_be_active")


@dataclass(frozen=True)
class CoordinatorTeamAssembly:
    snapshot: ProjectSnapshot
    context_source: str
    owner_task_text: str
    assembly_mode: str
    captain_role: str
    members: tuple[AssembledTeamMember, ...]
    project_specialist_roster: ProjectSpecialistRoster | None = None
    specialization_hints: SpecializationHints = field(
        default_factory=SpecializationHints.empty
    )

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in _VALID_TEAM_ASSEMBLY_CONTEXT_SOURCES
        ):
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_owner_task_text(self.owner_task_text),
        )
        if self.assembly_mode not in _VALID_ASSEMBLY_MODES:
            raise ValueError(f"invalid_assembly_mode:{self.assembly_mode!r}")
        if self.captain_role != COORDINATOR_ROLE:
            raise ValueError(f"invalid_captain_role:{self.captain_role!r}")
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("members_must_be_non_empty_tuple")
        if self.project_specialist_roster is None:
            object.__setattr__(
                self,
                "project_specialist_roster",
                ProjectSpecialistRoster(
                    project_id=self.snapshot.project.project_id,
                    specialist_roles=(),
                ),
            )
        elif not isinstance(self.project_specialist_roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_project_specialist_roster_type:"
                f"{type(self.project_specialist_roster).__name__}"
            )
        elif (
            self.project_specialist_roster.project_id
            != self.snapshot.project.project_id
        ):
            raise ValueError(
                "project_specialist_roster_project_id_mismatch:"
                f"{self.project_specialist_roster.project_id}!="
                f"{self.snapshot.project.project_id}"
            )
        if not isinstance(self.specialization_hints, SpecializationHints):
            raise ValueError(
                "invalid_specialization_hints_type:"
                f"{type(self.specialization_hints).__name__}"
            )

        member_roles: list[str] = []
        captain_count = 0
        for member in self.members:
            if not isinstance(member, AssembledTeamMember):
                raise ValueError(
                    "invalid_assembled_team_member_type:"
                    f"{type(member).__name__}"
                )
            member_roles.append(member.persona.agent_role)
            if member.is_captain:
                captain_count += 1
        if len(set(member_roles)) != len(member_roles):
            raise ValueError("duplicate_assembled_team_roles")
        if captain_count != 1:
            raise ValueError("assembled_team_requires_exactly_one_captain")
        if self.members[0].persona.agent_role != COORDINATOR_ROLE:
            raise ValueError("assembled_team_captain_must_be_first")
        if not self.members[0].is_captain:
            raise ValueError("assembled_team_first_member_must_be_captain")
        if tuple(member_roles) != BASELINE_INTERNAL_TEAM_ROLE_ORDER:
            raise ValueError("assembled_team_must_match_baseline_internal_team")


class CoordinatorTeamAssemblyService:
    @staticmethod
    def _baseline_role_order() -> tuple[str, ...]:
        return BASELINE_INTERNAL_TEAM_ROLE_ORDER

    @staticmethod
    def _role_mandate(role: str) -> str:
        if not isinstance(role, str) or role not in _ROLE_MANDATES:
            raise ValueError(f"unknown_team_assembly_role:{role!r}")
        return _ROLE_MANDATES[role]

    def _require_context(
        self,
        context: CoordinatorTeamAssemblyContext,
    ) -> CoordinatorTeamAssemblyContext:
        if not isinstance(context, CoordinatorTeamAssemblyContext):
            raise ValueError(
                "invalid_coordinator_team_assembly_context_type:"
                f"{type(context).__name__}"
            )
        if context.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        _validate_required_roles(context.personas)
        return context

    def _require_assembly(
        self,
        assembly: CoordinatorTeamAssembly,
    ) -> CoordinatorTeamAssembly:
        if not isinstance(assembly, CoordinatorTeamAssembly):
            raise ValueError(
                "invalid_coordinator_team_assembly_type:"
                f"{type(assembly).__name__}"
            )
        if assembly.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        return assembly

    def assemble_team(
        self,
        context: CoordinatorTeamAssemblyContext,
    ) -> CoordinatorTeamAssembly:
        context = self._require_context(context)
        members = tuple(
            AssembledTeamMember(
                persona=context.personas.for_role(role),
                mandate=self._role_mandate(role),
                is_captain=(role == COORDINATOR_ROLE),
            )
            for role in self._baseline_role_order()
        )
        return CoordinatorTeamAssembly(
            snapshot=context.snapshot,
            context_source=context.context_source,
            owner_task_text=context.owner_task_text,
            assembly_mode="baseline_internal_team",
            captain_role=COORDINATOR_ROLE,
            members=members,
            project_specialist_roster=context.project_specialist_roster,
            specialization_hints=context.specialization_hints,
        )

    def format_team_assembly(
        self,
        assembly: CoordinatorTeamAssembly,
    ) -> str:
        assembly = self._require_assembly(assembly)
        lines = [
            "Текущая assembled team",
            "",
            "Project anchor:",
            f"- project_id: {assembly.snapshot.project.project_id}",
            f"- slug: {assembly.snapshot.project.slug}",
            f"- name: {assembly.snapshot.project.name}",
            "",
            "Assembly mode:",
            f"- mode: {assembly.assembly_mode}",
            f"- context_source: {describe_context_source(assembly.context_source)}",
            f"- captain_role: {assembly.captain_role}",
            "",
            "Project specialists:",
        ]
        if assembly.project_specialist_roster.is_empty:
            lines.append("- none")
        else:
            for specialist_role in assembly.project_specialist_roster.specialist_roles:
                lines.append(f"- role_id: {specialist_role}")
        lines.extend(
            [
                "",
            "Specialization hints:",
            ]
        )
        if assembly.specialization_hints.is_empty:
            lines.append("- none")
        else:
            for hint in assembly.specialization_hints.items:
                lines.extend(
                    [
                        f"- specialist_role: {hint.specialist_role}",
                        f"  reason: {hint.reason}",
                    ]
                )
        lines.extend(
            [
                "",
            "Roster:",
            ]
        )
        for member in assembly.members:
            lines.extend(
                [
                    f"- role_id: {member.persona.agent_role}",
                    f"  human_name: {member.persona.human_name}",
                    f"  title: {member.persona.title}",
                    f"  seniority: {member.persona.seniority}",
                    f"  captain: {'yes' if member.is_captain else 'no'}",
                    f"  internal: {'yes' if member.is_internal else 'no'}",
                    f"  active: {'yes' if member.is_active else 'no'}",
                    f"  mandate: {member.mandate}",
                ]
            )
        return "\n".join(lines)

    def format_baseline_team_template(self, personas: PersonaRegistry) -> str:
        if not isinstance(personas, PersonaRegistry):
            raise ValueError(
                f"invalid_persona_registry_type:{type(personas).__name__}"
            )
        _validate_required_roles(personas)
        lines = [
            "Baseline internal team template",
            "",
            (
                "Это reference template, а не active assembled project team."
            ),
            "",
            "Project specialists:",
            "- none",
            "",
            "Roster:",
        ]
        for role in self._baseline_role_order():
            persona = personas.for_role(role)
            lines.extend(
                [
                    f"- role_id: {persona.agent_role}",
                    f"  human_name: {persona.human_name}",
                    f"  title: {persona.title}",
                    f"  seniority: {persona.seniority}",
                    f"  captain: {'yes' if role == COORDINATOR_ROLE else 'no'}",
                    "  internal: yes",
                    "  active: yes",
                    f"  mandate: {self._role_mandate(role)}",
                ]
            )
        return "\n".join(lines)
