"""
core/self_improvement.py

Step 12 of the ULTRA spec: error analysis, prompt optimization, feedback
loop. Pure-stdlib statistical layer on top of Observability — no LLM calls.
Reads `AgentCallRecord` history, normalises error messages into stable
signatures, groups them, computes per-agent failure stats, and emits
heuristic prompt-improvement suggestions plus a manual feedback log.

CONTRACTS:
1. All public dataclasses are frozen.
2. Error signatures are deterministic: numbers -> <NUM>, paths -> <PATH>,
   single/double-quoted strings -> <STR>, trimmed to MAX_SIG_LEN. Two
   ostensibly different errors that differ only in numbers/paths/quoted
   strings collapse to one signature.
3. Only patterns with occurrences >= MIN_OCCURRENCES (default 2) appear in
   ErrorAnalysis.patterns; one-off failures stay in AgentErrorStats.top_errors
   so callers can still see them, but they don't drive suggestions.
4. analyse_errors / suggest_prompt_improvements / feedback queries require
   an InMemorySink underneath Observability — non-memory sinks raise an
   explicit RuntimeError. Mirrors the same rule as Observability queries.
5. since_iso filters AgentCallRecord.timestamp >= since_iso (lexicographic
   compare on ISO-8601 strings is correct for this format).
6. Suggestions are sorted by priority desc (high > medium > low), then by
   pattern.occurrences desc, then by agent_name asc — fully deterministic.
7. record_feedback is append-only and in-memory; feedback_history returns a
   tuple snapshot, decoupled from internal state.
8. No network calls, no file I/O, no third-party deps.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from core.observability import AgentCallRecord, InMemorySink, Observability

MIN_OCCURRENCES = 2
MAX_SIG_LEN = 200

VALID_PRIORITIES = ("high", "medium", "low")
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

Priority = Literal["high", "medium", "low"]

VALID_RESOLUTIONS = (
    "fixed",
    "wont_fix",
    "duplicate",
    "external",
    "ignore",
    "investigating",
)


# ---------------------------------------------------------------------------
# Error signature normalisation
# ---------------------------------------------------------------------------


_RE_QUOTED_DOUBLE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')
_RE_QUOTED_SINGLE = re.compile(r"'[^'\\]*(?:\\.[^'\\]*)*'")
_RE_PATH = re.compile(r"(?:/[\w.\-]+)+|(?:[A-Za-z]:\\[\w.\-\\]+)")
_RE_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_RE_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_RE_WS = re.compile(r"\s+")


def normalise_error(message: str) -> str:
    """Reduce a raw error string to a stable signature.

    Replaces volatile parts (numbers, paths, quoted strings, hex) with
    placeholders so superficially-different errors collapse to one bucket.
    """
    if not isinstance(message, str):
        raise ValueError("non_string_message")
    text = message.strip()
    if not text:
        return ""
    text = _RE_QUOTED_DOUBLE.sub("<STR>", text)
    text = _RE_QUOTED_SINGLE.sub("<STR>", text)
    text = _RE_PATH.sub("<PATH>", text)
    text = _RE_HEX.sub("<HEX>", text)
    text = _RE_NUMBER.sub("<NUM>", text)
    text = _RE_WS.sub(" ", text)
    if len(text) > MAX_SIG_LEN:
        text = text[:MAX_SIG_LEN] + "..."
    return text


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorPattern:
    agent_name: str
    error_signature: str
    occurrences: int
    sample_errors: tuple[str, ...]
    avg_duration_ms_at_failure: float


@dataclass(frozen=True)
class AgentErrorStats:
    agent_name: str
    total_calls: int
    success_count: int
    failure_count: int
    failure_rate: float
    top_errors: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ErrorAnalysis:
    timestamp: str
    total_calls: int
    total_failures: int
    by_agent: Mapping[str, AgentErrorStats]
    patterns: tuple[ErrorPattern, ...]


@dataclass(frozen=True)
class PromptSuggestion:
    agent_name: str
    issue: str
    suggestion: str
    priority: str
    based_on_signature: str


@dataclass(frozen=True)
class FeedbackEntry:
    timestamp: str
    task_id: str
    agent_name: str
    error_signature: str
    resolution: str
    notes: str


# ---------------------------------------------------------------------------
# Heuristic suggestion engine
# ---------------------------------------------------------------------------

# Each rule: (pattern_substring_lowercased, issue, suggestion, priority).
# The list is iterated in order; first match wins per pattern.
_HEURISTIC_RULES: tuple[tuple[str, str, str, Priority], ...] = (
    (
        "json",
        "Agent returned invalid or unparseable JSON",
        "Add an explicit JSON schema example to the agent prompt and "
        "remind the model: 'return ONLY a valid JSON object, no markdown, "
        "no explanations'.",
        "high",
    ),
    (
        "timeout",
        "Agent timed out",
        "Increase the per-agent timeout, or split the task into smaller "
        "subtasks that fit within the existing budget.",
        "high",
    ),
    (
        "rate_limit",
        "Agent hit a rate limit",
        "Reduce concurrency or add exponential backoff in the router; "
        "consider a model with higher RPM allowance.",
        "high",
    ),
    (
        "unauthorized",
        "Agent received 401 from provider",
        "Verify OPENROUTER_API_KEY is loaded into the running env and "
        "is valid (use the /auth/key endpoint).",
        "high",
    ),
    (
        "empty",
        "Agent returned empty / blank output",
        "Add a concrete output example to the prompt and a refusal-policy "
        "reminder: 'never return an empty response'.",
        "medium",
    ),
    (
        "schema",
        "Agent output did not match required schema",
        "Pin the JSON schema in the prompt verbatim and add a self-check "
        "step: 'verify every required field is present before emitting'.",
        "medium",
    ),
    (
        "invalid_transition",
        "FSM rejected a transition produced by the agent",
        "Add the allowed transitions list to the agent prompt; explicitly "
        "list which next-states are legal from the current state.",
        "medium",
    ),
    (
        "blocked",
        "Agent emitted a BLOCKED marker",
        "Inspect the BLOCKED reason. If recurring, the prompt likely "
        "lacks input it expects; pre-resolve missing data or weaken the "
        "input requirement.",
        "medium",
    ),
    (
        "forbidden_token",
        "Agent emitted a forbidden token (TODO/FIXME/placeholder)",
        "Strengthen the no-placeholder rule in the prompt; add an explicit "
        "list: 'do not output TODO, FIXME, NotImplementedError, placeholder'.",
        "medium",
    ),
)


def _suggestion_for(pattern: ErrorPattern) -> PromptSuggestion | None:
    sig_lower = pattern.error_signature.lower()
    for needle, issue, suggestion, priority in _HEURISTIC_RULES:
        if needle in sig_lower:
            return PromptSuggestion(
                agent_name=pattern.agent_name,
                issue=issue,
                suggestion=suggestion,
                priority=priority,
                based_on_signature=pattern.error_signature,
            )
    # Fallback: low-priority generic suggestion when nothing matched.
    return PromptSuggestion(
        agent_name=pattern.agent_name,
        issue=f"Repeated unclassified error ({pattern.occurrences} times)",
        suggestion=(
            "Inspect the sample errors manually; consider tightening the "
            "agent prompt or adding an explicit failure-handling rule."
        ),
        priority="low",
        based_on_signature=pattern.error_signature,
    )


# ---------------------------------------------------------------------------
# SelfImprovement
# ---------------------------------------------------------------------------


@dataclass
class _MutableState:
    feedback: list[FeedbackEntry] = field(default_factory=list)


class SelfImprovement:
    def __init__(self, observability: Observability) -> None:
        if not isinstance(observability, Observability):
            raise ValueError(
                f"invalid_observability_type:{type(observability).__name__}"
            )
        self._obs = observability
        self._state = _MutableState()

    # ----- helpers --------------------------------------------------------

    def _require_memory(self, op: str) -> InMemorySink:
        sink = self._obs.sink
        if not isinstance(sink, InMemorySink):
            raise RuntimeError(f"{op}_requires_in_memory_sink")
        return sink

    @staticmethod
    def _filter_calls(
        calls: Iterable[AgentCallRecord],
        since_iso: str | None,
        agent_name: str | None,
    ) -> list[AgentCallRecord]:
        result: list[AgentCallRecord] = []
        for c in calls:
            if since_iso is not None and c.timestamp < since_iso:
                continue
            if agent_name is not None and c.agent_name != agent_name:
                continue
            result.append(c)
        return result

    # ----- error analysis -------------------------------------------------

    def analyse_errors(
        self,
        since_iso: str | None = None,
        agent_name: str | None = None,
        min_occurrences: int = MIN_OCCURRENCES,
    ) -> ErrorAnalysis:
        if not isinstance(min_occurrences, int) or min_occurrences < 1:
            raise ValueError(f"invalid_min_occurrences:{min_occurrences}")
        if since_iso is not None and (
            not isinstance(since_iso, str) or not since_iso.strip()
        ):
            raise ValueError("invalid_since_iso")
        if agent_name is not None and (
            not isinstance(agent_name, str) or not agent_name.strip()
        ):
            raise ValueError("invalid_agent_name")

        sink = self._require_memory("analyse_errors")
        calls = self._filter_calls(sink.calls, since_iso, agent_name)

        # Per-agent aggregation.
        per_agent_total: dict[str, int] = defaultdict(int)
        per_agent_failures: dict[str, int] = defaultdict(int)
        per_agent_error_counts: dict[str, Counter[str]] = defaultdict(Counter)
        # For pattern-level aggregation.
        sig_calls: dict[tuple[str, str], list[AgentCallRecord]] = defaultdict(list)

        for c in calls:
            per_agent_total[c.agent_name] += 1
            if c.ok:
                continue
            per_agent_failures[c.agent_name] += 1
            sig = normalise_error(c.error or "")
            if not sig:
                sig = "<empty_error>"
            per_agent_error_counts[c.agent_name][sig] += 1
            sig_calls[(c.agent_name, sig)].append(c)

        # AgentErrorStats per agent.
        by_agent: dict[str, AgentErrorStats] = {}
        for ag, total in per_agent_total.items():
            failures = per_agent_failures.get(ag, 0)
            success = total - failures
            top = tuple(per_agent_error_counts.get(ag, Counter()).most_common(5))
            rate = (failures / total) if total else 0.0
            by_agent[ag] = AgentErrorStats(
                agent_name=ag,
                total_calls=total,
                success_count=success,
                failure_count=failures,
                failure_rate=rate,
                top_errors=top,
            )

        # Patterns: only signatures hitting the occurrence threshold.
        patterns: list[ErrorPattern] = []
        for (ag, sig), records in sig_calls.items():
            occ = len(records)
            if occ < min_occurrences:
                continue
            samples = tuple(
                (r.error or "")[:MAX_SIG_LEN] for r in records[:3]
            )
            avg_ms = sum(r.duration_ms for r in records) / occ
            patterns.append(
                ErrorPattern(
                    agent_name=ag,
                    error_signature=sig,
                    occurrences=occ,
                    sample_errors=samples,
                    avg_duration_ms_at_failure=avg_ms,
                )
            )
        # Sort: most frequent first, then alphabetic by agent name for ties.
        patterns.sort(key=lambda p: (-p.occurrences, p.agent_name))

        return ErrorAnalysis(
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_calls=len(calls),
            total_failures=sum(per_agent_failures.values()),
            by_agent=dict(by_agent),
            patterns=tuple(patterns),
        )

    # ----- prompt suggestions --------------------------------------------

    def suggest_prompt_improvements(
        self, analysis: ErrorAnalysis
    ) -> tuple[PromptSuggestion, ...]:
        if not isinstance(analysis, ErrorAnalysis):
            raise ValueError(
                f"invalid_analysis_type:{type(analysis).__name__}"
            )

        suggestions: list[PromptSuggestion] = []
        for pattern in analysis.patterns:
            s = _suggestion_for(pattern)
            if s is not None:
                suggestions.append(s)

        # Promote agents with >50% failure rate to high priority via an
        # extra synthetic suggestion (one per such agent, deduped).
        seen_high_agents = {
            s.agent_name for s in suggestions if s.priority == "high"
        }
        for ag, stats in analysis.by_agent.items():
            if (
                stats.total_calls >= MIN_OCCURRENCES
                and stats.failure_rate > 0.5
                and ag not in seen_high_agents
            ):
                top_sig = stats.top_errors[0][0] if stats.top_errors else ""
                suggestions.append(
                    PromptSuggestion(
                        agent_name=ag,
                        issue=(
                            f"Agent {ag} has failure rate "
                            f"{stats.failure_rate:.0%} "
                            f"({stats.failure_count}/{stats.total_calls})"
                        ),
                        suggestion=(
                            "Major prompt rewrite required. Review the agent "
                            "prompt against recent failures, add concrete "
                            "examples, tighten output schema."
                        ),
                        priority="high",
                        based_on_signature=top_sig,
                    )
                )
                seen_high_agents.add(ag)

        suggestions.sort(
            key=lambda s: (
                _PRIORITY_RANK[s.priority],
                s.agent_name,
                s.based_on_signature,
            )
        )
        return tuple(suggestions)

    # ----- feedback log ---------------------------------------------------

    def record_feedback(
        self,
        task_id: str,
        agent_name: str,
        error_signature: str,
        resolution: str,
        notes: str = "",
    ) -> FeedbackEntry:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("empty_task_id")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("empty_agent_name")
        if not isinstance(error_signature, str) or not error_signature.strip():
            raise ValueError("empty_error_signature")
        if resolution not in VALID_RESOLUTIONS:
            raise ValueError(f"unknown_resolution:{resolution}")
        if not isinstance(notes, str):
            raise ValueError("invalid_notes_type")

        entry = FeedbackEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            task_id=task_id.strip(),
            agent_name=agent_name.strip(),
            error_signature=error_signature.strip(),
            resolution=resolution,
            notes=notes,
        )
        self._state.feedback.append(entry)
        return entry

    def feedback_history(
        self,
        agent_name: str | None = None,
        resolution: str | None = None,
    ) -> tuple[FeedbackEntry, ...]:
        if agent_name is not None and (
            not isinstance(agent_name, str) or not agent_name.strip()
        ):
            raise ValueError("invalid_agent_name")
        if resolution is not None and resolution not in VALID_RESOLUTIONS:
            raise ValueError(f"unknown_resolution:{resolution}")
        entries = self._state.feedback
        if agent_name is not None:
            entries = [e for e in entries if e.agent_name == agent_name]
        if resolution is not None:
            entries = [e for e in entries if e.resolution == resolution]
        return tuple(entries)
