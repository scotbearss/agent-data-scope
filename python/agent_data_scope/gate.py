"""Data scope gate.

One hook (``wrap_tool_call``) and one rule (permit by tool name), plus three
more things inside the same hook:

  - scope:    a permitted read is checked against the data that comes back.
              Records from any other case are dropped before the model sees
              them. If nothing is left, the read counts as blocked.
  - forbid:   fields on the forbid list are stripped from every result.
  - on_block: the agent's owner decides whether a blocked call stops the run
              (default) or lets the model continue with a refusal.

Every decision is one evidence line (names and counts, never data) and one
named step in the trace. At the end of the run a summary step is added.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableLambda

from .policy import OnBlock, ScopePolicy, highest_tier

ScopeContext = Mapping[str, Any]
"""What the gate knows about the job it is guarding. Values only, never data."""


def now_iso() -> str:
    """UTC timestamp in the same shape the TypeScript port writes (``...sss Z``)."""
    return iso(datetime.now(timezone.utc))


def iso(moment: datetime) -> str:
    """``Date.toISOString()`` shape: UTC, millisecond precision, trailing ``Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class GateDecision:
    """One line of evidence. Names and counts only, never the data itself."""

    at: str
    policy_version: int
    agent: str
    tool: str
    decision: str  # "permit" | "block"
    rule: str
    tier: str | None = None
    """Sensitivity tier declared for this source in the policy. Absent for unlisted tools."""
    records_checked: int | None = None
    records_dropped: int | None = None
    stripped_fields: tuple[str, ...] | None = None
    record_refs: tuple[str, ...] | None = None
    """Identifiers of the records the model was allowed to see. Names, never contents."""

    def to_dict(self) -> dict[str, Any]:
        """Evidence as plain data, absent fields omitted."""
        return {key: (list(value) if isinstance(value, tuple) else value) for key, value in asdict(self).items() if value is not None}


class DataScopeStopError(Exception):
    """Raised when a blocked call ends the run (``on_block: stop``)."""

    def __init__(self, message: str, gate_decision: GateDecision) -> None:
        super().__init__(message)
        self.gate_decision = gate_decision


@dataclass(frozen=True)
class LedgerSummary:
    highest_tier: str | None
    permitted: int
    blocked: int
    records_dropped: int
    fields_stripped: int


@dataclass
class ScopeLedger:
    """Collects decisions across one run so the outcome can be marked honestly."""

    policy: ScopePolicy | None = None
    decisions: list[GateDecision] = field(default_factory=list)

    def record(self, decision: GateDecision) -> None:
        self.decisions.append(decision)

    def summary(self) -> LedgerSummary:
        return LedgerSummary(
            highest_tier=highest_tier(self.policy, self.decisions) if self.policy else None,
            permitted=sum(1 for d in self.decisions if d.decision == "permit"),
            blocked=sum(1 for d in self.decisions if d.decision == "block"),
            records_dropped=sum(d.records_dropped or 0 for d in self.decisions),
            fields_stripped=sum(len(d.stripped_fields or ()) for d in self.decisions),
        )

    def mark(self) -> str:
        totals = self.summary()
        notes = [
            f"{totals.blocked} blocked read{'' if totals.blocked == 1 else 's'}" if totals.blocked else "",
            f"{totals.records_dropped} record{'' if totals.records_dropped == 1 else 's'} dropped" if totals.records_dropped else "",
            f"{totals.fields_stripped} field{'' if totals.fields_stripped == 1 else 's'} stripped" if totals.fields_stripped else "",
        ]
        notes = [note for note in notes if note]
        return f"completed with {', '.join(notes)}" if notes else "completed within scope"


def create_scope_ledger(policy: ScopePolicy | None = None) -> ScopeLedger:
    return ScopeLedger(policy=policy)


# ---- helpers -------------------------------------------------------------

_SCOPE = re.compile(r"^\s*([a-z_]+)\s*==\s*([a-z_]+(?:\.[a-z_]+)+)\s*$")


def normalize_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def read_field(record: Any, field_name: str) -> Any:
    if not isinstance(record, Mapping):
        return None
    wanted = normalize_key(field_name)
    for key, value in record.items():
        if isinstance(key, str) and normalize_key(key) == wanted:
            return value
    return None


def parse_scope(scope: str) -> tuple[str, list[str]]:
    match = _SCOPE.match(scope)
    if not match:
        raise ValueError(f"Unsupported scope expression: {scope}")
    return match.group(1), match.group(2).split(".")


def resolve_context(context: ScopeContext, path: Sequence[str]) -> str | None:
    current: Any = context
    for segment in path:
        if isinstance(current, Mapping):
            current = current.get(segment)
        elif current is not None and not isinstance(current, (str, bytes)):
            current = getattr(current, segment, None)
        else:
            current = None
    return current if isinstance(current, str) and current else None


def strip_forbidden(value: Any, forbidden: frozenset[str], removed: list[str]) -> Any:
    """Remove forbidden keys anywhere in a JSON value. Appends the names removed."""
    if isinstance(value, list):
        return [strip_forbidden(item, forbidden, removed) for item in value]
    if not isinstance(value, Mapping):
        return value
    output: dict[str, Any] = {}
    for key, child in value.items():
        if isinstance(key, str) and normalize_key(key) in forbidden:
            removed.append(key)
            continue
        output[key] = strip_forbidden(child, forbidden, removed)
    return output


def trace_step(run_name: str, policy_version: int, tier: str | None = None) -> None:
    """One named step in the trace. Context variables carry the parent run, so it nests."""
    tags = ["data-scope-gate", f"policy-v{policy_version}"] + ([f"tier-{tier}"] if tier else [])
    RunnableLambda(lambda _: run_name).with_config(run_name=run_name, tags=tags).invoke({})


_UNPARSED = object()


@dataclass(frozen=True)
class _Scope:
    field: str
    expected: str
    path: str


@dataclass(frozen=True)
class _Rule:
    tier: str | None = None
    scope: _Scope | None = None


# ---- the gate ------------------------------------------------------------


class DataScopeGate(AgentMiddleware):
    """The middleware. Build it with :func:`create_data_scope_gate`."""

    def __init__(
        self,
        policy: ScopePolicy,
        agent: str,
        context: ScopeContext | None = None,
        on_decision: Callable[[GateDecision], None] | None = None,
    ) -> None:
        block = policy.agents.get(agent)
        if block is None:
            raise ValueError(f"Scope policy v{policy.version} has no block for agent {agent}")
        self.policy = policy
        self.agent = agent
        self.on_block: OnBlock = block.on_block or policy.defaults.on_block
        self.forbidden = frozenset(normalize_key(name) for name in policy.forbid.fields_everywhere)
        self.on_decision = on_decision
        context = context or {}

        # Resolve every scope up front, so a missing context value fails when the
        # agent is built, not in the middle of a run.
        self.rules: dict[str, _Rule] = {}
        for rule in block.permit:
            if not rule.tool:
                continue
            if not rule.scope:
                self.rules[rule.tool] = _Rule(tier=rule.tier)
                continue
            field_name, path = parse_scope(rule.scope)
            expected = resolve_context(context, path)
            if not expected:
                raise ValueError(f"Scope for {agent}.{rule.tool} needs context {'.'.join(path)}")
            self.rules[rule.tool] = _Rule(tier=rule.tier, scope=_Scope(field=field_name, expected=expected, path=".".join(path)))

        self.local: list[GateDecision] = []

    @property
    def name(self) -> str:
        # Becomes a graph node name, so it must not contain ":".
        return f"DataScopeGate-{self.agent}"

    # -- evidence ----------------------------------------------------------

    def _stamp(self, decision: GateDecision) -> None:
        self.local.append(decision)
        if self.on_decision:
            self.on_decision(decision)

    def _base(self, tool: str, tier: str | None = None) -> dict[str, Any]:
        return {"at": now_iso(), "policy_version": self.policy.version, "agent": self.agent, "tool": tool, "tier": tier}

    def _refuse(self, decision: GateDecision, request: Any) -> ToolMessage:
        self._stamp(decision)
        trace_step(f"DataScopeGate block {decision.tool} | {decision.rule}", self.policy.version, decision.tier)
        message = f"Blocked by data scope policy v{self.policy.version}: {decision.tool} for {self.agent} ({decision.rule})."
        if self.on_block == "stop":
            raise DataScopeStopError(f"Data scope gate stopped the run. {message}", decision)
        return ToolMessage(tool_call_id=request.tool_call.get("id") or "", name=decision.tool, status="error", content=message)

    # -- the decision, split so the sync and async hooks share it -----------

    def _before(self, request: Any) -> tuple[_Rule | None, ToolMessage | None]:
        """Permit by name. Returns (rule, refusal). Unlisted tools never run."""
        tool = request.tool_call["name"]
        rule = self.rules.get(tool)
        if rule is not None:
            return rule, None
        if self.policy.defaults.unlisted_tools == "block":
            return None, self._refuse(GateDecision(**self._base(tool), decision="block", rule="defaults.unlisted_tools"), request)
        self._stamp(GateDecision(**self._base(tool), decision="permit", rule="defaults.unlisted_tools"))
        trace_step(f"DataScopeGate permit {tool} | defaults.unlisted_tools", self.policy.version)
        return None, None

    def _after(self, request: Any, rule: _Rule, result: Any) -> Any:
        tool = request.tool_call["name"]
        permit_rule = f"agents.{self.agent}.permit"
        if not isinstance(result, ToolMessage) or not isinstance(result.content, str):
            self._stamp(GateDecision(**self._base(tool, rule.tier), decision="permit", rule=permit_rule))
            trace_step(f"DataScopeGate permit {tool}", self.policy.version, rule.tier)
            return result

        try:
            parsed: Any = json.loads(result.content)
        except ValueError:
            parsed = _UNPARSED

        # 2. Scope. A permitted read is checked against what came back.
        records_checked: int | None = None
        records_dropped: int | None = None
        record_refs: tuple[str, ...] | None = None
        if rule.scope:
            container = parsed if isinstance(parsed, Mapping) and isinstance(parsed.get("records"), list) else None
            records = container["records"] if container is not None else (None if parsed is _UNPARSED else [parsed])
            if records is None:
                return self._refuse(GateDecision(**self._base(tool, rule.tier), decision="block", rule=f"{permit_rule}.scope unverifiable"), request)
            kept = [record for record in records if read_field(record, rule.scope.field) == rule.scope.expected]
            records_checked = len(records)
            records_dropped = len(records) - len(kept)
            if not kept:
                return self._refuse(
                    GateDecision(**self._base(tool, rule.tier), decision="block", rule=f"{permit_rule}.scope", records_checked=records_checked, records_dropped=records_dropped),
                    request,
                )
            parsed = {**container, "records": kept} if container is not None else kept[0]
            refs = [read_field(record, "ref") for record in kept]
            if all(isinstance(ref, str) and ref for ref in refs):
                record_refs = tuple(refs)

        # 3. Forbid. Listed fields are stripped before the model sees the result.
        stripped: list[str] = []
        if parsed is not _UNPARSED:
            parsed = strip_forbidden(parsed, self.forbidden, stripped)

        decision = GateDecision(
            **self._base(tool, rule.tier),
            decision="permit",
            rule=permit_rule,
            records_checked=records_checked,
            records_dropped=records_dropped,
            record_refs=record_refs,
            stripped_fields=tuple(stripped) if stripped else None,
        )
        self._stamp(decision)
        notes = [f"dropped {records_dropped}" if records_dropped else "", f"stripped {','.join(stripped)}" if stripped else ""]
        notes = [note for note in notes if note]
        trace_step(f"DataScopeGate permit {tool}" + (f" | {' | '.join(notes)}" if notes else ""), self.policy.version, rule.tier)

        if parsed is _UNPARSED or (records_checked is None and not stripped):
            return result
        return ToolMessage(tool_call_id=result.tool_call_id, name=result.name or tool, status=result.status, content=json.dumps(parsed))

    # -- hooks --------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rule, refusal = self._before(request)
        if refusal is not None:
            return refusal
        if rule is None:
            return handler(request)
        return self._after(request, rule, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        rule, refusal = self._before(request)
        if refusal is not None:
            return refusal
        if rule is None:
            return await handler(request)
        return self._after(request, rule, await handler(request))

    def _summary_step(self) -> None:
        permitted = sum(1 for d in self.local if d.decision == "permit")
        blocked = sum(1 for d in self.local if d.decision == "block")
        dropped = sum(d.records_dropped or 0 for d in self.local)
        stripped = sum(len(d.stripped_fields or ()) for d in self.local)
        top = highest_tier(self.policy, self.local)
        trace_step(
            f"DataScopeGate summary {self.agent} | permitted {permitted} | blocked {blocked} | dropped {dropped} | stripped {stripped}" + (f" | highest tier {top}" if top else ""),
            self.policy.version,
            top,
        )

    def after_agent(self, state: Any, runtime: Any) -> None:
        self._summary_step()
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        self._summary_step()
        return None


def create_data_scope_gate(
    policy: ScopePolicy,
    agent: str,
    context: ScopeContext | None = None,
    on_decision: Callable[[GateDecision], None] | None = None,
) -> DataScopeGate:
    """Build the gate for one agent. Fails now, not mid-run, if the policy or context is short."""
    return DataScopeGate(policy, agent, context=context, on_decision=on_decision)
