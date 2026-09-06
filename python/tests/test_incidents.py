"""Port of data-scope-incidents.test.ts: three deterministic rules, owner from
policy, append-only record with forward-only states, feedback key for LangSmith
alerts, fleet report section."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_data_scope.fleet_report import Period, build_fleet_report, render_fleet_report
from agent_data_scope.gate import GateDecision
from agent_data_scope.incidents import RunForIncidents, detect_run_incidents, incident_feedback, open_incidents, read_incidents, transition_incident
from agent_data_scope.lesson import LESSON_AGENT, LESSON_SCI_TOOL, create_lesson_agent, run_lesson, scripted_lesson_model
from agent_data_scope.policy import ScopePolicy

AT = "2026-09-06T21:00:00.000Z"
DRAFTER = "example-support-drafter"


def decision(tool: str, verdict: str, **partial) -> GateDecision:  # noqa: ANN003
    return GateDecision(**{"at": AT, "policy_version": 1, "agent": DRAFTER, "tool": tool, "decision": verdict, "rule": f"agents.{DRAFTER}.permit", **partial})


def run(**overrides) -> RunForIncidents:  # noqa: ANN003
    base = dict(run_id="44444444-4444-4444-8444-444444444444", run_name="Support draft", started_at=AT, integrity="ok", necessity=0.5, prior_necessity=[], decisions=[])
    return RunForIncidents(**{**base, **overrides})


def test_sensitive_gate_action(policy: ScopePolicy) -> None:
    sci = detect_run_incidents(run(decisions=[decision("get_incoming_message", "permit", tier="SCI", stripped_fields=("phone_number",))]), policy)
    assert len(sci) == 1
    assert sci[0].type == "sensitive_gate_action"
    assert sci[0].owner == "platform-team", "the drafter names no owner, so the default applies"
    assert "stripped phone_number" in sci[0].detail
    assert sci[0].id == f"sensitive_gate_action:{sci[0].run_id}:{DRAFTER}:get_incoming_message"
    bci = detect_run_incidents(run(decisions=[decision("get_incident_snapshot", "permit", tier="BCI", stripped_fields=("phone_number",))]), policy)
    assert bci == [], "the same strip on BCI is a statistic"
    clean = detect_run_incidents(run(decisions=[decision("get_incoming_message", "permit", tier="SCI", records_checked=1, records_dropped=0)]), policy)
    assert clean == [], "a clean read of a sensitive source is not an incident"
    blocked = detect_run_incidents(run(decisions=[decision("get_incoming_message", "block", tier="SCI", rule=f"agents.{DRAFTER}.permit.scope")]), policy)
    assert blocked[0].type == "sensitive_gate_action" and blocked[0].detail == f"blocked get_incoming_message (agents.{DRAFTER}.permit.scope)"
    dropped = detect_run_incidents(run(decisions=[decision("get_incoming_message", "permit", tier="SCI", records_checked=2, records_dropped=1)]), policy)
    assert dropped[0].detail == "permitted get_incoming_message, dropped 1"


def test_owner_comes_from_the_agent_block_when_named(policy: ScopePolicy) -> None:
    investigator = replace(decision("get_incident_snapshot", "permit", tier="SCI", stripped_fields=("x",)), agent="example-investigator")
    [incident] = detect_run_incidents(run(decisions=[investigator]), policy)
    assert incident.owner == "ops-team"


def test_integrity(policy: ScopePolicy) -> None:
    flagged = detect_run_incidents(run(integrity="cited_not_fetched", decisions=[decision("get_incoming_message", "permit", tier="SCI")]), policy)
    assert [incident.type for incident in flagged] == ["integrity"]
    assert flagged[0].agent == DRAFTER and flagged[0].tier is None


def test_necessity_streak_needs_the_full_window(policy: ScopePolicy) -> None:
    decisions = [decision("get_incoming_message", "permit", tier="SCI")]

    def streaks(necessity: float, prior: list) -> list:
        return [i for i in detect_run_incidents(run(decisions=decisions, necessity=necessity, prior_necessity=prior), policy) if i.type == "necessity_streak"]

    assert len(streaks(0.1, [0.2, 0.1])) == 1
    assert streaks(0.1, [0.2, 0.1])[0].detail == "necessity below 0.25 for 3 runs in a row (0.20, 0.10, 0.10)"
    assert streaks(0.1, [0.9, 0.1]) == [], "one good run breaks the streak"
    assert streaks(0.1, [0.1]) == [], "not enough runs yet"
    assert streaks(0.1, [None, 0.1]) == [], "an unscored run does not count as low"
    assert len(streaks(0.1, [0.5, 0.2, 0.1])) == 1, "only the last N runs matter"


def test_no_incident_rules_means_no_incidents(policy: ScopePolicy) -> None:
    without = policy.model_dump()
    without["incidents"] = None
    assert detect_run_incidents(run(integrity="cited_not_fetched", decisions=[decision("x", "block", tier="SCI")]), ScopePolicy.model_validate(without)) == []


def test_feedback_for_langsmith_alerts(policy: ScopePolicy) -> None:
    incidents = detect_run_incidents(run(decisions=[decision("get_incoming_message", "permit", tier="SCI", stripped_fields=("message_text",))]), policy)
    feedback = incident_feedback(incidents, policy)
    assert feedback["key"] == "data_scope_incident_v1"
    assert feedback["score"] == 1
    assert f"sensitive_gate_action [{DRAFTER}, SCI] permitted get_incoming_message, stripped message_text" in feedback["comment"]
    assert incident_feedback([], policy) == {"key": "data_scope_incident_v1", "score": 0, "comment": "No data scope incident on this run."}


def test_record_opens_dedupes_moves_forward_and_reports(tmp_path, policy: ScopePolicy) -> None:  # noqa: ANN001
    path = os.path.join(tmp_path, "incidents.jsonl")
    incidents = detect_run_incidents(run(decisions=[decision("get_incoming_message", "permit", tier="SCI", stripped_fields=("message_text",))]), policy)
    assert len(open_incidents(path, incidents, AT)) == 1
    assert open_incidents(path, incidents, AT) == [], "already on file"
    incident_id = incidents[0].id
    assert read_incidents(path)[0].state == "open"

    # The event log uses the TypeScript port's keys.
    events = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    assert events[0]["kind"] == "opened" and events[0]["at"] == AT
    assert set(events[0]["incident"]) == {"id", "type", "runId", "runName", "startedAt", "agent", "tier", "detail", "owner", "policyVersion"}

    transition_incident(path, incident_id, "acknowledged", "looking into the tool", AT)
    with pytest.raises(ValueError, match="already acknowledged"):
        transition_incident(path, incident_id, "open", None, AT)
    with pytest.raises(ValueError, match="No incident with id"):
        transition_incident(path, "nope", "closed", None, AT)
    transition_incident(path, incident_id, "closed", None, AT)
    record = read_incidents(path)[0]
    assert record.state == "closed"
    assert [entry.state for entry in record.history] == ["open", "acknowledged", "closed"]
    assert record.history[1].note == "looking into the tool"
    events = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    assert events[1] == {"kind": "state", "at": AT, "id": incident_id, "state": "acknowledged", "note": "looking into the tool"}
    assert events[2] == {"kind": "state", "at": AT, "id": incident_id, "state": "closed"}

    report = render_fleet_report(build_fleet_report([], "test", Period(since=datetime.fromtimestamp(0, timezone.utc), until=datetime(2026, 9, 6, 21, tzinfo=timezone.utc)), policy, read_incidents(path)))
    assert "Open 0, acknowledged 0, remediated 0, closed 1" in report
    assert re.search(rf"\| closed \| sensitive_gate_action \| `{DRAFTER}` \| SCI \| 2026-09-06 21:00 \| platform-team \| permitted get_incoming_message, stripped message_text \|", report)
    assert "must never reach" not in report, "no data on the report"


def test_typescript_written_events_fold(tmp_path) -> None:  # noqa: ANN001
    path = os.path.join(tmp_path, "ts.jsonl")
    incident = {"id": "integrity:run-1", "type": "integrity", "runId": "run-1", "runName": "TS", "startedAt": AT, "agent": "lead", "detail": "d", "owner": "founder", "policyVersion": 4}
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "opened", "at": AT, "incident": incident}) + "\n")
        handle.write(json.dumps({"kind": "state", "at": AT, "id": "integrity:run-1", "state": "remediated"}) + "\n")
        handle.write(json.dumps({"kind": "state", "at": AT, "id": "unknown", "state": "closed"}) + "\n")
    [record] = read_incidents(path)
    assert (record.state, record.policy_version, record.tier, record.run_id) == ("remediated", 4, None, "run-1")


def test_lesson_sci_probe_strip_becomes_an_incident(policy_path: str) -> None:
    session = create_lesson_agent(scripted_lesson_model(sci_probe=True), on_block="continue", policy_path=policy_path, sci_probe=True)
    outcome = run_lesson(session)
    assert outcome.ran["sci_tool"] == 1
    sci = next(d for d in outcome.decisions if d.tool == LESSON_SCI_TOOL)
    assert (sci.decision, sci.tier, sci.stripped_fields) == ("permit", "SCI", ("message_text",))
    seen = next(m for m in outcome.tool_messages if m.name == LESSON_SCI_TOOL)
    assert "must never reach" not in str(seen.content), "the forbidden field never reached the model"
    assert outcome.summary.highest_tier == "SCI"
    incidents = detect_run_incidents(
        RunForIncidents(run_id="55555555-5555-4555-8555-555555555555", run_name="lesson", started_at=AT, decisions=outcome.decisions, necessity=outcome.necessity.overall.necessity, integrity=outcome.necessity.overall.integrity, prior_necessity=[]),
        session.policy,
    )
    assert [(i.type, i.agent, i.tier) for i in incidents] == [("sensitive_gate_action", LESSON_AGENT, "SCI")]
    assert incidents[0].owner == "ops-team"
