"""Fleet report. Governance does not read runs; it asks how often the agents
followed their rules over a period, across the fleet.

The report is built from records that already exist: the gate's named trace
steps (tagged with policy version and tier) and the necessity feedback. The
default source is LangSmith, because that is where every other trace already
goes. A local ledger source exists for teams that prefer their own store. Both
produce the same record shape, so the report cannot tell them apart, and
neither carries data: names, counts, tiers and scores only.

The ledger JSONL uses the TypeScript port's keys (``runId``, ``startedAt``,
``policyVersion``, ``highestTier``) so either port can read the other's file.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from .gate import iso
from .incidents import IncidentRecord
from .policy import ScopePolicy, highest_tier


@dataclass(frozen=True)
class AgentSummary:
    agent: str
    permitted: int
    blocked: int
    dropped: int
    stripped: int
    highest_tier: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = {"agent": self.agent, "permitted": self.permitted, "blocked": self.blocked, "dropped": self.dropped, "stripped": self.stripped}
        if self.highest_tier:
            data["highestTier"] = self.highest_tier
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AgentSummary":
        return cls(agent=data["agent"], permitted=data["permitted"], blocked=data["blocked"], dropped=data["dropped"], stripped=data["stripped"], highest_tier=data.get("highestTier"))


@dataclass(frozen=True)
class FleetRunRecord:
    run_id: str
    run_name: str
    started_at: str
    policy_version: int | None
    agents: tuple[AgentSummary, ...]
    necessity: float | None
    integrity: str  # "ok" | "cited_not_fetched" | "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "runName": self.run_name,
            "startedAt": self.started_at,
            "policyVersion": self.policy_version,
            "agents": [agent.to_json() for agent in self.agents],
            "necessity": self.necessity,
            "integrity": self.integrity,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FleetRunRecord":
        return cls(
            run_id=data["runId"],
            run_name=data["runName"],
            started_at=data["startedAt"],
            policy_version=data.get("policyVersion"),
            agents=tuple(AgentSummary.from_json(agent) for agent in data.get("agents", [])),
            necessity=data.get("necessity"),
            integrity=data.get("integrity", "unknown"),
        )


@dataclass(frozen=True)
class Period:
    since: datetime
    until: datetime


class DecisionSource(Protocol):
    name: str

    def runs(self, period: Period) -> list[FleetRunRecord]: ...


# ---- parsing the gate's trace step names ---------------------------------

_SUMMARY = re.compile(r"^DataScopeGate summary (\S+) \| permitted (\d+) \| blocked (\d+) \| dropped (\d+) \| stripped (\d+)(?: \| highest tier (\S+))?$")


def parse_summary_step(name: str) -> AgentSummary | None:
    match = _SUMMARY.match(name)
    if not match:
        return None
    return AgentSummary(agent=match.group(1), permitted=int(match.group(2)), blocked=int(match.group(3)), dropped=int(match.group(4)), stripped=int(match.group(5)), highest_tier=match.group(6))


def policy_version_from_tags(tags: Sequence[str] | None) -> int | None:
    for tag in tags or []:
        match = re.match(r"^policy-v(\d+)$", tag)
        if match:
            return int(match.group(1))
    return None


def integrity_from_comment(comment: str | None) -> str:
    match = re.search(r"integrity (ok|cited_not_fetched)", comment or "")
    return match.group(1) if match else "unknown"


# ---- sources -------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_at(text: str) -> datetime:
    return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))


@dataclass
class LangSmithDecisionSource:
    """Reads root runs in a period, their child chain runs (the gate's steps), and necessity feedback."""

    client: Any
    project_names: Sequence[str]

    @property
    def name(self) -> str:
        return f"langsmith:{','.join(self.project_names)}"

    def runs(self, period: Period) -> list[FleetRunRecord]:
        records: list[FleetRunRecord] = []
        for project_name in self.project_names:
            project = self.client.read_project(project_name=project_name)
            project_id = str(project.id)
            traces = self.client.traces.query(
                project_id=project_id,
                min_start_time=iso(period.since),
                max_start_time=iso(period.until),
                selects=["ID", "NAME", "START_TIME", "TAGS"],
                page_size=100,
            )
            for trace in traces:
                root = getattr(trace, "root_run", None)
                if root is None or not getattr(root, "id", None):
                    continue
                children = self.client.traces.list_runs(str(root.id), project_id=project_id, filter='eq(run_type, "chain")', selects=["ID", "NAME", "RUN_TYPE", "TAGS", "START_TIME"])
                items = list(getattr(children, "items", None) or [])
                agents = [summary for summary in (parse_summary_step(getattr(item, "name", "") or "") for item in items) if summary is not None]
                if not agents:
                    continue  # a run without the gate is not part of the fleet
                versions = [version for version in (policy_version_from_tags(getattr(item, "tags", None)) for item in items) if version is not None]
                necessity: float | None = None
                integrity = "unknown"
                for feedback in self.client.list_feedback(run_ids=[str(root.id)], feedback_key=["data_necessity_v1"]):
                    score = getattr(feedback, "score", None)
                    necessity = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
                    integrity = integrity_from_comment(getattr(feedback, "comment", None))
                start = getattr(root, "start_time", None)
                records.append(
                    FleetRunRecord(
                        run_id=str(root.id),
                        run_name=getattr(root, "name", None) or "",
                        started_at=iso(start) if isinstance(start, datetime) else str(start or ""),
                        policy_version=max(versions) if versions else None,
                        agents=tuple(agents),
                        necessity=necessity,
                        integrity=integrity,
                    )
                )
        return sorted(records, key=lambda record: record.started_at, reverse=True)


def langsmith_decision_source(client: Any, project_names: Sequence[str]) -> LangSmithDecisionSource:
    return LangSmithDecisionSource(client=client, project_names=list(project_names))


def append_fleet_run_record(path: str, record: FleetRunRecord) -> None:
    """One JSON record per line. Written by the run itself, read back by the report."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_json()) + "\n")


@dataclass
class LedgerDecisionSource:
    path: str

    @property
    def name(self) -> str:
        return f"ledger:{self.path}"

    def runs(self, period: Period) -> list[FleetRunRecord]:
        if not os.path.exists(self.path):
            return []
        since, until = _aware(period.since), _aware(period.until)
        with open(self.path, encoding="utf-8") as handle:
            records = [FleetRunRecord.from_json(json.loads(line)) for line in handle.read().split("\n") if line.strip()]
        kept = [record for record in records if since <= _parse_at(record.started_at) <= until]
        return sorted(kept, key=lambda record: record.started_at, reverse=True)


def ledger_decision_source(path: str) -> LedgerDecisionSource:
    return LedgerDecisionSource(path=path)


# ---- the report ----------------------------------------------------------


@dataclass(frozen=True)
class FleetTotals:
    runs: int
    clean: int  # the gate had nothing to catch
    gate_acted: int  # at least one block, drop or strip
    blocked: int
    dropped: int
    stripped: int
    necessity_scored: int
    necessity_average: float | None
    integrity_flags: int
    highest_tier: str | None = None


@dataclass(frozen=True)
class FleetAgentRow:
    agent: str
    runs: int
    clean: int
    blocked: int
    dropped: int
    stripped: int
    highest_tier: str | None = None


@dataclass(frozen=True)
class GateActedRun:
    started_at: str
    run_id: str
    run_name: str
    blocked: int
    dropped: int
    stripped: int
    integrity: str


@dataclass(frozen=True)
class IncidentCounts:
    open: int
    acknowledged: int
    remediated: int
    closed: int
    rows: tuple[IncidentRecord, ...]


@dataclass(frozen=True)
class FleetReport:
    source: str
    period_since: str
    period_until: str
    policy_versions: tuple[int, ...]
    totals: FleetTotals
    agents: tuple[FleetAgentRow, ...]
    gate_acted_runs: tuple[GateActedRun, ...]
    incidents: IncidentCounts


def _acted(record: FleetRunRecord) -> bool:
    return any(agent.blocked + agent.dropped + agent.stripped > 0 for agent in record.agents)


def build_fleet_report(records: Sequence[FleetRunRecord], source: str, period: Period, policy: ScopePolicy | None = None, incidents: Sequence[IncidentRecord] = ()) -> FleetReport:
    def total(pick: Callable[[AgentSummary], int]) -> int:
        return sum(pick(agent) for record in records for agent in record.agents)

    def top(tiers: Sequence[str | None]) -> str | None:
        if policy:
            return highest_tier(policy, tiers)
        present = sorted(tier for tier in tiers if tier)
        return present[-1] if present else None

    by_agent: dict[str, dict[str, Any]] = {}
    for record in records:
        for agent in record.agents:
            row = by_agent.setdefault(agent.agent, {"runs": 0, "clean": 0, "blocked": 0, "dropped": 0, "stripped": 0, "tiers": []})
            row["runs"] += 1
            if agent.blocked + agent.dropped + agent.stripped == 0:
                row["clean"] += 1
            row["blocked"] += agent.blocked
            row["dropped"] += agent.dropped
            row["stripped"] += agent.stripped
            row["tiers"].append(agent.highest_tier)

    scored = [record for record in records if record.necessity is not None]
    return FleetReport(
        source=source,
        period_since=iso(period.since),
        period_until=iso(period.until),
        policy_versions=tuple(sorted({record.policy_version for record in records if record.policy_version is not None})),
        totals=FleetTotals(
            runs=len(records),
            clean=sum(1 for record in records if not _acted(record)),
            gate_acted=sum(1 for record in records if _acted(record)),
            blocked=total(lambda agent: agent.blocked),
            dropped=total(lambda agent: agent.dropped),
            stripped=total(lambda agent: agent.stripped),
            necessity_scored=len(scored),
            necessity_average=sum(record.necessity or 0 for record in scored) / len(scored) if scored else None,
            integrity_flags=sum(1 for record in records if record.integrity == "cited_not_fetched"),
            highest_tier=top([agent.highest_tier for record in records for agent in record.agents]),
        ),
        agents=tuple(
            FleetAgentRow(agent=agent, runs=row["runs"], clean=row["clean"], blocked=row["blocked"], dropped=row["dropped"], stripped=row["stripped"], highest_tier=top(row["tiers"]))
            for agent, row in sorted(by_agent.items())
        ),
        gate_acted_runs=tuple(
            GateActedRun(
                started_at=record.started_at,
                run_id=record.run_id,
                run_name=record.run_name,
                blocked=sum(agent.blocked for agent in record.agents),
                dropped=sum(agent.dropped for agent in record.agents),
                stripped=sum(agent.stripped for agent in record.agents),
                integrity=record.integrity,
            )
            for record in records
            if _acted(record)
        ),
        incidents=IncidentCounts(
            open=sum(1 for i in incidents if i.state == "open"),
            acknowledged=sum(1 for i in incidents if i.state == "acknowledged"),
            remediated=sum(1 for i in incidents if i.state == "remediated"),
            closed=sum(1 for i in incidents if i.state == "closed"),
            rows=tuple(incidents),
        ),
    )


def _pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{int(math.floor(part / whole * 100 + 0.5))}%"


def _when(text: str) -> str:
    return text[:16].replace("T", " ")


def render_fleet_report(report: FleetReport) -> str:
    t = report.totals
    lines: list[str] = []
    lines.append("# Data scope fleet report")
    lines.append("")
    lines.append(f"Period {report.period_since[:10]} to {report.period_until[:10]}. Source: {report.source}. Policy versions seen: {', '.join(str(v) for v in report.policy_versions) or 'none'}.")
    lines.append("")
    lines.append(
        'Every run below ran behind the data scope gate, so every rule was enforced on every tool call. "Clean" means the gate had nothing to catch. "Gate acted" means it blocked a call, dropped out-of-scope records, or stripped forbidden fields, and the run was marked accordingly. Counts only; no data appears in this report.'
    )
    lines.append("")
    lines.append("## Across the fleet")
    lines.append("")
    lines.append("| Runs | Clean | Gate acted | Blocked calls | Records dropped | Fields stripped | Necessity (avg) | Integrity flags | Highest tier |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    average = "n/a" if t.necessity_average is None else f"{t.necessity_average:.2f}"
    lines.append(
        f"| {t.runs} | {t.clean} ({_pct(t.clean, t.runs)}) | {t.gate_acted} ({_pct(t.gate_acted, t.runs)}) | {t.blocked} | {t.dropped} | {t.stripped} | {average} over {t.necessity_scored} | {t.integrity_flags} | {t.highest_tier or 'n/a'} |"
    )
    lines.append("")
    lines.append("## Per agent")
    lines.append("")
    lines.append("| Agent | Runs | Clean | Blocked | Dropped | Stripped | Highest tier |")
    lines.append("|---|---|---|---|---|---|---|")
    for agent in report.agents:
        lines.append(f"| `{agent.agent}` | {agent.runs} | {agent.clean} ({_pct(agent.clean, agent.runs)}) | {agent.blocked} | {agent.dropped} | {agent.stripped} | {agent.highest_tier or 'n/a'} |")
    lines.append("")
    lines.append("## Runs where the gate acted")
    lines.append("")
    if not report.gate_acted_runs:
        lines.append("None in this period.")
    else:
        lines.append("| When | Run | Blocked | Dropped | Stripped | Integrity |")
        lines.append("|---|---|---|---|---|---|")
        for run in report.gate_acted_runs:
            lines.append(f"| {_when(run.started_at)} | {run.run_name} (`{run.run_id[:8]}`) | {run.blocked} | {run.dropped} | {run.stripped} | {run.integrity} |")
    lines.append("")
    lines.append("## Incidents and remediation")
    lines.append("")
    i = report.incidents
    lines.append(
        f"Open {i.open}, acknowledged {i.acknowledged}, remediated {i.remediated}, closed {i.closed}. An incident is a gate action on a sensitive-tier source, a report citing evidence the gate never let through, or necessity below the floor for several runs in a row. Each has an owner and moves open, acknowledged, remediated, closed."
    )
    lines.append("")
    if not i.rows:
        lines.append("None on file.")
    else:
        lines.append("| State | Type | Agent | Tier | When | Owner | Detail |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in i.rows:
            lines.append(f"| {row.state} | {row.type} | `{row.agent}` | {row.tier or 'n/a'} | {_when(row.started_at)} | {row.owner} | {row.detail} |")
    lines.append("")
    return "\n".join(lines)
