"""Routing — pick the model for a call by the *nature* of the call.

"Select by capability need" needs a definition or it degenerates into taste, so
capability is expressed as a concrete `CallKind` mapped to hard requirements and
soft preferences. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
delta 10, and [§5](../../TRADEOFFS.md#5-model-routing-3-tier-haiku--sonnet--opus)
for why routing is keyed on task nature rather than on a pipeline phase — phases
died with the graph pivot, and the nature of a call predicts required capability
better than its position ever did.

**The family-difference rule is a correctness requirement, not a preference.**
[EVAL.md](../../EVAL.md) requires the judge to differ from the agent and
[SECURITY.md](../../SECURITY.md) L3 requires a different-family reviewer. A
configuration that violates it therefore fails at wiring time — waiting until the
first judged run would mean discovering it after producing a batch of invalid
scores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .provider_catalog import MODELS, ModelSpec, Tier, family_of, model


class CallKind(str, Enum):
    """What a call is *for*. The routing key.

    Deliberately not "phase" — there are no phases. These are roles a call plays,
    and a single investigation may make several kinds.
    """

    MAIN_LOOP = "main_loop"
    REFUTE = "refute"
    JUDGE = "judge"
    REVIEWER = "reviewer"
    CLASSIFY = "classify"


#: Kinds that must not share a model family with the agent. Both requirements come
#: from documents outside this module, which is why they are named here rather
#: than left implicit in a config file.
MUST_DIFFER_FROM_AGENT: frozenset[CallKind] = frozenset({CallKind.JUDGE, CallKind.REVIEWER})


@dataclass(frozen=True)
class Requirements:
    """Hard constraints. A candidate failing any of these is not a candidate."""

    needs_tools: bool = False
    needs_reliable_tool_use: bool = False
    min_context: int = 0
    #: Set by the router for judge/reviewer; a candidate of this family is rejected.
    excluded_family: str | None = None


REQUIREMENTS: dict[CallKind, Requirements] = {
    # The loop dies without valid tool_use, so tool-call reliability is a hard
    # requirement here and nowhere else.
    CallKind.MAIN_LOOP: Requirements(needs_tools=True, needs_reliable_tool_use=True, min_context=32_000),
    CallKind.REFUTE: Requirements(needs_tools=True, min_context=32_000),
    CallKind.JUDGE: Requirements(min_context=16_000),
    CallKind.REVIEWER: Requirements(min_context=8_000),
    CallKind.CLASSIFY: Requirements(min_context=4_000),
}

PREFERRED_TIER: dict[CallKind, Tier] = {
    CallKind.MAIN_LOOP: "workhorse",
    CallKind.REFUTE: "strong",
    CallKind.JUDGE: "strong",
    CallKind.REVIEWER: "workhorse",
    CallKind.CLASSIFY: "cheap",
}


class RoutingError(RuntimeError):
    """Raised at wiring time. Never at request time — a routing table that cannot
    satisfy a kind is a configuration bug, and discovering it mid-incident is
    strictly worse than refusing to start."""


@dataclass(frozen=True)
class RoutingConfig:
    """Which model serves each kind, plus per-kind fallback chains.

    `agent_model` is the reference point for the family-difference rule: it is the
    model whose family judge and reviewer must avoid.
    """

    agent_model: str
    assignments: dict[CallKind, str] = field(default_factory=dict)
    #: Ordered fallback candidates per kind, tried only on provider unavailability.
    fallbacks: dict[CallKind, tuple[str, ...]] = field(default_factory=dict)

    def model_for(self, kind: CallKind) -> str:
        return self.assignments.get(kind, self.agent_model)

    def candidates(self, kind: CallKind) -> tuple[str, ...]:
        """Primary first, then fallbacks. Deduplicated, order preserved."""
        chain = (self.model_for(kind), *self.fallbacks.get(kind, ()))
        seen: dict[str, None] = {}
        for model_id in chain:
            seen.setdefault(model_id, None)
        return tuple(seen)


def satisfies(spec: ModelSpec, req: Requirements) -> tuple[bool, str]:
    """Whether a model meets the hard constraints, and why not if it does not."""
    if req.needs_tools and not spec.supports_tools:
        return False, f"{spec.id} does not support tool use"
    if req.needs_reliable_tool_use and not spec.reliable_tool_use:
        return False, f"{spec.id} is marked unreliable for tool use"
    if spec.context_window < req.min_context:
        return False, f"{spec.id} context {spec.context_window} < required {req.min_context}"
    if req.excluded_family and family_of(spec.id) == req.excluded_family:
        return False, (
            f"{spec.id} is family {req.excluded_family!r}, which must differ from the agent's"
        )
    return True, ""


def requirements_for(kind: CallKind, agent_model: str) -> Requirements:
    base = REQUIREMENTS[kind]
    if kind in MUST_DIFFER_FROM_AGENT:
        return Requirements(
            needs_tools=base.needs_tools,
            needs_reliable_tool_use=base.needs_reliable_tool_use,
            min_context=base.min_context,
            excluded_family=family_of(agent_model),
        )
    return base


def validate(config: RoutingConfig) -> None:
    """Check every kind and every fallback candidate. Called from `Gateway.__init__`.

    Every violation is collected before raising, so a misconfiguration is fixed in
    one pass rather than one error at a time.
    """
    problems: list[str] = []

    if config.agent_model not in MODELS:
        raise RoutingError(f"agent_model {config.agent_model!r} is not in the catalog")

    for kind in CallKind:
        req = requirements_for(kind, config.agent_model)
        for model_id in config.candidates(kind):
            if model_id not in MODELS:
                problems.append(f"{kind.value}: unknown model {model_id!r}")
                continue
            ok, why = satisfies(model(model_id), req)
            if not ok:
                problems.append(f"{kind.value}: {why}")

    if problems:
        raise RoutingError(
            "routing configuration is invalid:\n  - " + "\n  - ".join(problems)
        )


def route(config: RoutingConfig, kind: CallKind) -> tuple[ModelSpec, ...]:
    """Ordered candidates for a kind: primary first, then fallbacks.

    Assumes `validate()` has already passed, so this cannot fail at request time.
    Fallback candidates are returned but the gateway decides whether to use them —
    they are disabled during eval, because a run that continued on a second
    provider produces an accuracy figure attributable to neither model.
    """
    return tuple(model(model_id) for model_id in config.candidates(kind))


def default_config(agent_model: str = "deepseek-chat", judge_model: str = "claude-sonnet-5") -> RoutingConfig:
    """The Tier 1.5 wiring: one primary carrying all tuning, one different family
    for judge and reviewer, cheap tier for classification.

    Deliberately two tiers rather than three — a cheap-model noise classifier is
    premature until measurement shows fingerprint plus vector similarity is
    insufficient ([§5](../../TRADEOFFS.md#5-model-routing-3-tier-haiku--sonnet--opus)).
    `CLASSIFY` is wired anyway so the seam exists.
    """
    return RoutingConfig(
        agent_model=agent_model,
        assignments={
            CallKind.MAIN_LOOP: agent_model,
            CallKind.REFUTE: agent_model,
            CallKind.JUDGE: judge_model,
            CallKind.REVIEWER: judge_model,
            CallKind.CLASSIFY: "qwen-turbo",
        },
    )
