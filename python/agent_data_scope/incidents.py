"""Incidents. Three deterministic rules turn a gate event into something with an
owner and a state, instead of a statistic:

  sensitive_gate_action  the gate blocked, dropped or stripped on a source whose
                         tier is in incidents.sensitive_tiers. Nothing reached
                         the model; something upstream drifted.
  integrity              a report cited evidence the gate never let through.
  necessity_streak       necessity below the floor for N runs in a row.

Incidents are recorded locally as an append-only event log (open, then state
changes) and posted to LangSmith as feedback so a LangSmith alert can notify
people. Records carry names, counts and identifiers, never data.

The JSONL on disk uses the same keys as the TypeScript port (``runId``,
``startedAt``, ...) so either port can read the other's file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from .gate import GateDecision, now_iso
from .necessity import wait_for_run
from .policy import ScopePolicy

INCIDENT_STATES: tuple[str, ...] = ("open", "acknowledged", "remediated", "closed")
DEFAULT_INCIDENT_FEEDBACK_KEY = "data_scope_incident_v1"


@dataclass(frozen=True)
class Incident:
    id: str
    type: str  # sensitive_gate_action | integrity | necessity_streak
    run_id: str
    run_name: str
    started_at: str
    agent: str
    detail: str
    owner: str
    policy_version: int
    tier: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "type": self.type,
            "runId": self.run_id,
            "runName": self.run_name,
            "startedAt": self.started_at,
            "agent": self.agent,
            "detail": self.detail,
            "owner": self.owner,
            "policyVersion": self.policy_version,
        }
        if self.tier:
            data["tier"] = self.tier
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Incident":
        return cls(
            id=data["id"],
            type=data["type"],
            run_id=data["runId"],
            run_name=data["runName"],
            started_at=data["startedAt"],
            agent=data["agent"],
            detail=data["detail"],
            owner=data["owner"],
            policy_version=data["policyVersion"],
            tier=data.get("tier"),
        )


@dataclass(frozen=True)
class IncidentHistoryEntry:
    at: str
    state: str
    note: str | None = None


@dataclass(frozen=True)
class IncidentRecord(Incident):
    state: str = "open"
    history: tuple[IncidentHistoryEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RunForIncidents:
    run_id: str
    run_name: str
    started_at: str
    decisions: Sequence[GateDecision]
    necessity: float | None
    integrity: str  # "ok" | "cited_not_fetched" | "unknown"
    prior_necessity: Sequence[float | None] = ()
    """Necessity of earlier runs, oldest first. Used for the streak rule."""


def detect_run_incidents(run: RunForIncidents, policy: ScopePolicy) -> list[Incident]:
    rules = policy.incidents
    if rules is None:
        return []

    def owner(agent: str) -> str:
        block = policy.agents.get(agent)
        return (block.owner if block and block.owner else None) or rules.owner

    sensitive = set(rules.sensitive_tiers)
    first = run.decisions[0] if run.decisions else None
    base = {
        "run_id": run.run_id,
        "run_name": run.run_name,
        "started_at": run.started_at,
        "policy_version": first.policy_version if first else policy.version,
    }
    incidents: list[Incident] = []

    for decision in run.decisions:
        if not decision.tier or decision.tier not in sensitive:
            continue
        acted = decision.decision == "block" or (decision.records_dropped or 0) > 0 or bool(decision.stripped_fields)
        if not acted:
            continue
        if decision.decision == "block":
            what = f"blocked {decision.tool} ({decision.rule})"
        else:
            parts = [
                f"permitted {decision.tool}",
                f"dropped {decision.records_dropped}" if decision.records_dropped else "",
                f"stripped {','.join(decision.stripped_fields)}" if decision.stripped_fields else "",
            ]
            what = ", ".join(part for part in parts if part)
        incidents.append(
            Incident(
                **base,
                id=f"sensitive_gate_action:{run.run_id}:{decision.agent}:{decision.tool}",
                type="sensitive_gate_action",
                agent=decision.agent,
                tier=decision.tier,
                detail=what,
                owner=owner(decision.agent),
            )
        )

    if run.integrity == "cited_not_fetched":
        agent = first.agent if first else "unknown"
        incidents.append(Incident(**base, id=f"integrity:{run.run_id}", type="integrity", agent=agent, detail="a report cited evidence the gate did not let through", owner=owner(agent)))

    prior = list(run.prior_necessity)
    tail = prior[len(prior) - (rules.necessity_streak - 1) :] if rules.necessity_streak > 1 else []
    window = [*tail, run.necessity]
    if len(window) == rules.necessity_streak and all(score is not None and score < rules.necessity_floor for score in window):
        agent = first.agent if first else "unknown"
        scores = ", ".join(f"{(score or 0):.2f}" for score in window)
        incidents.append(
            Incident(
                **base,
                id=f"necessity_streak:{run.run_id}",
                type="necessity_streak",
                agent=agent,
                detail=f"necessity below {rules.necessity_floor} for {rules.necessity_streak} runs in a row ({scores})",
                owner=owner(agent),
            )
        )
    return incidents


# ---- the record: an append-only event log, folded on read ---------------


def read_incident_events(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().split("\n") if line.strip()]


def fold_incidents(events: Sequence[dict[str, Any]]) -> list[IncidentRecord]:
    records: dict[str, IncidentRecord] = {}
    for event in events:
        if event.get("kind") == "opened":
            incident = Incident.from_json(event["incident"])
            if incident.id not in records:
                records[incident.id] = IncidentRecord(**incident.__dict__, state="open", history=(IncidentHistoryEntry(at=event["at"], state="open"),))
        elif event.get("kind") == "state":
            current = records.get(event["id"])
            if current is not None:
                entry = IncidentHistoryEntry(at=event["at"], state=event["state"], note=event.get("note"))
                records[event["id"]] = replace(current, state=event["state"], history=(*current.history, entry))
    return sorted(records.values(), key=lambda record: record.started_at, reverse=True)


def read_incidents(path: str) -> list[IncidentRecord]:
    return fold_incidents(read_incident_events(path))


def _append(path: str, event: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def open_incidents(path: str, incidents: Sequence[Incident], at: str | None = None) -> list[Incident]:
    """Opens incidents that are not already on file. Returns the ones newly opened."""
    at = at or now_iso()
    existing = {record.id for record in read_incidents(path)}
    opened: list[Incident] = []
    for incident in incidents:
        if incident.id in existing:
            continue
        _append(path, {"kind": "opened", "at": at, "incident": incident.to_json()})
        opened.append(incident)
    return opened


def transition_incident(path: str, id: str, state: str, note: str | None = None, at: str | None = None) -> IncidentRecord:
    at = at or now_iso()
    current = next((record for record in read_incidents(path) if record.id == id), None)
    if current is None:
        raise ValueError(f"No incident with id {id}")
    if state not in INCIDENT_STATES:
        raise ValueError(f"state must be one of {', '.join(INCIDENT_STATES)}")
    if INCIDENT_STATES.index(state) <= INCIDENT_STATES.index(current.state):
        raise ValueError(f"Incident {id} is already {current.state}; cannot move to {state}")
    event: dict[str, Any] = {"kind": "state", "at": at, "id": id, "state": state}
    if note:
        event["note"] = note
    _append(path, event)
    return replace(current, state=state)


# ---- LangSmith feedback, the signal a LangSmith alert watches -------------


def incident_feedback(incidents: Sequence[Incident], policy: ScopePolicy) -> dict[str, Any]:
    key = policy.incidents.feedback_key if policy.incidents else DEFAULT_INCIDENT_FEEDBACK_KEY
    if not incidents:
        comment = "No data scope incident on this run."
    else:
        described = "; ".join(f"{i.type} [{i.agent}{f', {i.tier}' if i.tier else ''}] {i.detail}" for i in incidents)
        comment = f"{len(incidents)} data scope incident{'' if len(incidents) == 1 else 's'}: {described}. Names and counts only."
    return {"key": key, "score": 1 if incidents else 0, "comment": comment}


def record_incident_feedback(client: Any, run_id: str, project_name: str, incidents: Sequence[Incident], policy: ScopePolicy) -> dict[str, Any]:
    project = client.read_project(project_name=project_name)
    waited_ms = wait_for_run(client, run_id, str(project.id), 60_000)
    feedback = incident_feedback(incidents, policy)
    client.create_feedback(
        run_id,
        feedback["key"],
        score=feedback["score"],
        comment=feedback["comment"],
        feedback_source_type="api",
        project_id=project.id,
        extend_trace_retention=False,
    )
    return {**feedback, "waited_ms": waited_ms}
