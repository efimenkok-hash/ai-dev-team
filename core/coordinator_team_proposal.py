from __future__ import annotations

from core.coordinator_onboarding import describe_context_source
from core.coordinator_team_assembly import CoordinatorTeamAssembly


class CoordinatorTeamProposalService:
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

    def build_team_proposal_artifact(
        self,
        assembly: CoordinatorTeamAssembly,
    ) -> str:
        assembly = self._require_assembly(assembly)
        captain = assembly.members[0]
        lines = [
            "Coordinator team proposal",
            "",
            "Project anchor:",
            f"- project_id: {assembly.snapshot.project.project_id}",
            f"- slug: {assembly.snapshot.project.slug}",
            f"- name: {assembly.snapshot.project.name}",
            "",
            "Context mode:",
            f"- mode: {describe_context_source(assembly.context_source)}",
            "",
            "Assembly source:",
            f"- assembly_mode: {assembly.assembly_mode}",
            f"- captain_role: {assembly.captain_role}",
            "",
            "Specialization hints:",
        ]
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
            "Project captain:",
            f"- role_id: {captain.persona.agent_role}",
            f"  human_name: {captain.persona.human_name}",
            f"  title: {captain.persona.title}",
            f"  seniority: {captain.persona.seniority}",
            (
                "  mandate: project captain and control-plane lead; "
                "coordinates the team instead of executing the whole pipeline alone."
            ),
            "",
            "Proposed internal team:",
            ]
        )
        for member in assembly.members:
            lines.extend(
                [
                    f"- role_id: {member.persona.agent_role}",
                    f"  human_name: {member.persona.human_name}",
                    f"  title: {member.persona.title}",
                    f"  seniority: {member.persona.seniority}",
                    f"  mandate: {member.mandate}",
                ]
            )
        lines.extend(
            [
                "",
                "Owner task anchor:",
                "<<<OWNER_TASK",
                assembly.owner_task_text,
                "OWNER_TASK",
                "",
                "Scope and non-goals:",
                "This proposal is anchored to the current project contour only.",
                "Do not invent another project or runtime contour.",
                "Hiring and external roles are not auto-activated at this step.",
            ]
        )
        return "\n".join(lines)
