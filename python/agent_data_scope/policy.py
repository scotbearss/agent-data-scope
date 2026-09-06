"""Scope policy: the YAML file, validated once, never at run time.

Three kinds of rule live in the policy:
  permit  = hard. The gate allows this and blocks everything not listed.
  forbid  = hard, and it wins over permit. Listed fields are stripped from tool
            results before the model ever sees them.
  expect  = soft. Not enforced. Checked after the run and scored.

Every permitted source carries a sensitivity tier declared by the agent's owner
and approved by governance. The gate does not guess sensitivity from content; it
enforces and reports on the declaration.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

OnBlock = Literal["stop", "continue"]


class _Strict(BaseModel):
    """Mirror of zod's strictObject: unknown keys are an error."""

    model_config = ConfigDict(extra="forbid")


class PermitRule(_Strict):
    tool: str | None = None
    read: str | None = None
    tier: str | None = None
    fields: list[str] | None = None
    scope: str | None = None


class Tier(_Strict):
    label: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class Classification(_Strict):
    scheme: str = Field(min_length=1)
    tiers: list[Tier] = Field(min_length=1)


class ChangeLogEntry(_Strict):
    version: int = Field(gt=0)
    on: str = Field(min_length=1)
    change: str = Field(min_length=1)


class Approval(_Strict):
    status: Literal["draft", "approved", "changes_requested"]
    owner: str = Field(min_length=1)
    approved_by: str | None
    approved_on: str | None
    change_log: list[ChangeLogEntry] = Field(min_length=1)


class AgentBlock(_Strict):
    permit: list[PermitRule] = Field(default_factory=list)
    expect: list[dict[str, str]] | None = None
    on_block: OnBlock | None = None
    owner: str | None = Field(default=None, min_length=1)


class IncidentRules(_Strict):
    owner: str = Field(min_length=1)
    sensitive_tiers: list[str] = Field(min_length=1)
    necessity_floor: float = Field(ge=0, le=1)
    necessity_streak: int = Field(gt=0)
    feedback_key: str = Field(min_length=1)


class Defaults(_Strict):
    unlisted_tools: Literal["block", "permit"]
    writes: Literal["forbid"]
    on_block: OnBlock = "stop"


class Forbid(_Strict):
    fields_everywhere: list[str]


class ScopePolicy(_Strict):
    version: int = Field(gt=0)
    agent_group: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    summary: list[str] | None = None
    classification: Classification | None = None
    approval: Approval | None = None
    incidents: IncidentRules | None = None
    defaults: Defaults
    forbid: Forbid
    agents: dict[str, AgentBlock]

    @model_validator(mode="after")
    def _tiers_are_declared(self) -> "ScopePolicy":
        # A declared tier must be one the organization defined, and once a
        # classification exists every permitted source must carry a tier.
        labels = {tier.label for tier in self.classification.tiers} if self.classification else set()
        issues: list[str] = []
        for tier in (self.incidents.sensitive_tiers if self.incidents else []):
            if tier not in labels:
                issues.append(f"incidents.sensitive_tiers: {tier} is not in the classification")
        for agent, block in self.agents.items():
            for rule in block.permit:
                source = rule.tool or rule.read or "?"
                if rule.tier and rule.tier not in labels:
                    issues.append(f"{agent}.{source}: tier {rule.tier} is not in the classification")
                if self.classification and not rule.tier:
                    issues.append(f"{agent}.{source}: missing tier")
        if issues:
            raise ValueError("; ".join(issues))
        return self


class _StringDatesLoader(yaml.SafeLoader):
    """PyYAML follows YAML 1.1: `2026-09-06` becomes a date and the key `on` becomes `True`.

    The TypeScript port's parser follows YAML 1.2, where both stay strings. Dropping
    the timestamp resolver and narrowing booleans to true/false keeps both ports
    reading the same values from the same file.
    """


_DROPPED_TAGS = {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool"}
_StringDatesLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag not in _DROPPED_TAGS]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StringDatesLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def parse_scope_policy(text: str) -> ScopePolicy:
    """Validate policy YAML text. A malformed policy fails here, never at run time."""
    return ScopePolicy.model_validate(yaml.load(text, Loader=_StringDatesLoader))


def load_scope_policy(path: str) -> ScopePolicy:
    """Read and validate a policy file."""
    with open(path, encoding="utf-8") as handle:
        return parse_scope_policy(handle.read())


def tier_rank(policy: ScopePolicy, label: str | None) -> int:
    """Rank of a tier label in the policy's classification, 0 = least sensitive, -1 = none."""
    if not label or not policy.classification:
        return -1
    for index, tier in enumerate(policy.classification.tiers):
        if tier.label == label:
            return index
    return -1


def _tier_of(item: Any) -> str | None:
    if item is None or isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return item.get("tier")
    return getattr(item, "tier", None)


def highest_tier(policy: ScopePolicy, decisions: Iterable[Any]) -> str | None:
    """The most sensitive tier among decisions (objects, dicts, or labels), by the policy's own ordering."""
    best: str | None = None
    for item in decisions:
        tier = _tier_of(item)
        if tier_rank(policy, tier) > tier_rank(policy, best):
            best = tier
    return best
