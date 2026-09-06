"""Port of data-scope-gate.test.ts: scope drops foreign records, forbid strips
fields, stop ends the run, continue marks it, necessity is scored without a model."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tracers.base import BaseTracer

from agent_data_scope.gate import DataScopeStopError, GateDecision, ScopeLedger, create_data_scope_gate, create_scope_ledger, strip_forbidden
from agent_data_scope.lesson import LESSON_AGENT, LESSON_INCIDENT, create_lesson_agent, run_lesson, scripted_lesson_model
from agent_data_scope.necessity import necessity_feedback, score_necessity
from agent_data_scope.policy import ScopePolicy


def unwrap(error: BaseException) -> DataScopeStopError | None:
    """The framework may wrap errors raised from middleware; the original is the cause."""
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, DataScopeStopError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


class CollectingTracer(BaseTracer):
    def __init__(self) -> None:
        super().__init__()
        self.runs: list = []

    def _persist_run(self, run) -> None:  # noqa: ANN001
        pass

    def _on_run_create(self, run) -> None:  # noqa: ANN001
        self.runs.append(run)


def test_gate_cannot_be_built_for_unknown_agent(policy: ScopePolicy) -> None:
    with pytest.raises(ValueError, match="no block for agent"):
        create_data_scope_gate(policy, "example-unknown")


def test_scoped_permit_needs_context_at_build_time(policy: ScopePolicy) -> None:
    with pytest.raises(ValueError, match=r"needs context incident\.id"):
        create_data_scope_gate(policy, LESSON_AGENT)
    with pytest.raises(ValueError, match=r"needs context incident\.id"):
        create_data_scope_gate(policy, LESSON_AGENT, context={"incident": {"id": ""}})


def test_scope_grammar_is_checked(policy: ScopePolicy) -> None:
    broken = policy.model_dump()
    broken["agents"][LESSON_AGENT]["permit"][0]["scope"] = "incident_id in incident.ids"
    with pytest.raises(ValueError, match="Unsupported scope expression"):
        create_data_scope_gate(ScopePolicy.model_validate(broken), LESSON_AGENT, context={"incident": {"id": "x"}})


def test_middleware_name_has_no_colon(policy: ScopePolicy) -> None:
    gate = create_data_scope_gate(policy, LESSON_AGENT, context={"incident": {"id": LESSON_INCIDENT}})
    assert gate.name == f"DataScopeGate-{LESSON_AGENT}"
    assert ":" not in gate.name


def test_default_on_block_stop_ends_the_run(policy_path: str) -> None:
    session = create_lesson_agent(scripted_lesson_model(), policy_path=policy_path)
    with pytest.raises(Exception) as caught:
        run_lesson(session)
    stop = unwrap(caught.value)
    assert stop is not None, f"expected DataScopeStopError, got {caught.value!r}"
    assert "get_household_records" in str(stop)
    assert stop.gate_decision.rule == "defaults.unlisted_tools"
    assert session.ran["forbidden_tool"] == 0, "the blocked tool never ran"
    assert any(decision.decision == "block" for decision in session.ledger.decisions)


def test_continue_marks_the_run_and_scores_necessity(policy_path: str) -> None:
    session = create_lesson_agent(scripted_lesson_model(), on_block="continue", policy_path=policy_path)
    outcome = run_lesson(session)

    assert len(outcome.decisions) == 2, "the gate decided on both tool calls"
    permit = next(d for d in outcome.decisions if d.tool == "get_incident_snapshot")
    block = next(d for d in outcome.decisions if d.tool == "get_household_records")
    assert permit.decision == "permit"
    assert permit.rule == f"agents.{LESSON_AGENT}.permit"
    assert permit.tier == "BCI", "the decision carries the declared tier"
    assert block.tier is None, "an unlisted tool has no declared tier"
    assert outcome.summary.highest_tier == "BCI"
    assert permit.records_checked == 3, "all records were checked against the incident"
    assert permit.records_dropped == 1, "the record from another incident was dropped"
    assert permit.stripped_fields == ("email_address",), "the forbidden field was stripped"
    assert block.decision == "block"
    assert block.rule == "defaults.unlisted_tools"
    assert outcome.ran["permitted_tool"] == 1
    assert outcome.ran["forbidden_tool"] == 0

    # What the model actually saw: right incident only, no forbidden field.
    seen = next(m for m in outcome.tool_messages if m.name == "get_incident_snapshot")
    body = json.loads(str(seen.content))
    assert len(body["records"]) == 2
    assert all(record["incidentId"] == LESSON_INCIDENT for record in body["records"])
    assert "email_address" not in body["records"][0]
    assert permit.record_refs == ("snapshot:fictional:signals", "snapshot:fictional:heartbeat"), "evidence names the records the model saw"
    assert "example.invalid" not in str(seen.content), "the stripped value is gone"

    refused = next(m for m in outcome.tool_messages if m.name == "get_household_records")
    assert refused.status == "error"
    assert "Blocked by data scope policy v2" in str(refused.content)
    assert "Fictional report" in outcome.final_text

    # Deterministic necessity. Two records were seen, one was cited.
    assert outcome.cited_refs == ["snapshot:fictional:signals"]
    scored = next(a for a in outcome.necessity.agents if a.agent == LESSON_AGENT)
    assert scored.measurable is True
    assert scored.used == ("snapshot:fictional:signals",)
    assert scored.unused == ("snapshot:fictional:heartbeat",), "fetched and never cited"
    assert scored.cited_not_fetched == ()
    assert scored.necessity == 0.5
    assert outcome.necessity.overall.__dict__ == {"fetched": 2, "used": 1, "unused": 1, "necessity": 0.5, "integrity": "ok"}
    feedback = necessity_feedback(outcome.necessity)
    assert feedback["key"] == "data_necessity_v1"
    assert feedback["score"] == 0.5
    assert f"Unused: {LESSON_AGENT}: snapshot:fictional:heartbeat" in feedback["comment"]
    assert "fresh" not in feedback["comment"] and "example.invalid" not in feedback["comment"], "feedback carries names, never data"

    # Integrity: a report that cites something the gate never let through is flagged.
    tampered = score_necessity(outcome.decisions, {LESSON_AGENT: ["snapshot:fictional:signals", "snapshot:fictional-other:platform"]})
    assert tampered.overall.integrity == "cited_not_fetched"
    assert tampered.agents[0].cited_not_fetched == ("snapshot:fictional-other:platform",)
    assert outcome.mark == "completed with 1 blocked read, 1 record dropped, 1 field stripped"
    assert outcome.summary.__dict__ == {"highest_tier": "BCI", "permitted": 1, "blocked": 1, "records_dropped": 1, "fields_stripped": 1}

    # Evidence lines carry names and counts only, never values.
    evidence = json.dumps([decision.to_dict() for decision in outcome.decisions])
    assert "snapshot:fictional:heartbeat" in evidence, "record identifiers are evidence"
    assert "fresh" not in evidence and "example.invalid" not in evidence and "delivery_failure" not in evidence, "values never are"


def test_trace_steps_are_named_and_tagged(policy_path: str) -> None:
    session = create_lesson_agent(scripted_lesson_model(), on_block="continue", policy_path=policy_path)
    tracer = CollectingTracer()
    run_lesson(session, {"callbacks": [tracer]})
    steps = {run.name: run for run in tracer.runs if run.name.startswith("DataScopeGate ")}
    assert "DataScopeGate permit get_incident_snapshot | dropped 1 | stripped email_address" in steps
    assert "DataScopeGate block get_household_records | defaults.unlisted_tools" in steps
    assert f"DataScopeGate summary {LESSON_AGENT} | permitted 1 | blocked 1 | dropped 1 | stripped 1 | highest tier BCI" in steps
    permit = steps["DataScopeGate permit get_incident_snapshot | dropped 1 | stripped email_address"]
    assert {"data-scope-gate", "policy-v2", "tier-BCI"} <= set(permit.tags)
    assert permit.parent_run_id is not None, "the step nests under the agent's run"
    block = steps["DataScopeGate block get_household_records | defaults.unlisted_tools"]
    assert "tier-" not in " ".join(block.tags)


class _Request:
    def __init__(self, name: str, call_id: str = "call-1") -> None:
        self.tool_call = {"name": name, "args": {}, "id": call_id}


def _gate(policy: ScopePolicy, ledger: ScopeLedger, on_block: str = "continue"):
    copy = policy.model_dump()
    copy["agents"][LESSON_AGENT]["on_block"] = on_block
    return create_data_scope_gate(ScopePolicy.model_validate(copy), LESSON_AGENT, context={"incident": {"id": LESSON_INCIDENT}}, on_decision=ledger.record)


def test_scope_blocks_when_nothing_is_left(policy: ScopePolicy) -> None:
    ledger = create_scope_ledger(policy)
    gate = _gate(policy, ledger)
    foreign = json.dumps({"records": [{"ref": "r", "incident_id": "inc-other"}]})
    result = gate.wrap_tool_call(_Request("get_incident_snapshot"), lambda req: ToolMessage(content=foreign, tool_call_id="call-1", name="get_incident_snapshot"))
    assert isinstance(result, ToolMessage) and result.status == "error"
    decision = ledger.decisions[-1]
    assert (decision.decision, decision.rule, decision.records_checked, decision.records_dropped) == ("block", f"agents.{LESSON_AGENT}.permit.scope", 1, 1)
    assert ledger.mark() == "completed with 1 blocked read, 1 record dropped"


def test_scope_blocks_unverifiable_results(policy: ScopePolicy) -> None:
    ledger = create_scope_ledger(policy)
    gate = _gate(policy, ledger)
    result = gate.wrap_tool_call(_Request("get_incident_snapshot"), lambda req: ToolMessage(content="not json", tool_call_id="call-1", name="get_incident_snapshot"))
    assert result.status == "error"
    assert ledger.decisions[-1].rule == f"agents.{LESSON_AGENT}.permit.scope unverifiable"


def test_single_record_result_is_scoped_by_normalized_key(policy: ScopePolicy) -> None:
    ledger = create_scope_ledger(policy)
    gate = _gate(policy, ledger)
    single = json.dumps({"ref": "one", "Incident-ID": LESSON_INCIDENT, "person_name": "x", "nested": [{"Phone_Number": "y", "ok": 1}]})
    result = gate.wrap_tool_call(_Request("get_incident_snapshot"), lambda req: ToolMessage(content=single, tool_call_id="call-1", name="get_incident_snapshot"))
    assert json.loads(result.content) == {"ref": "one", "Incident-ID": LESSON_INCIDENT, "nested": [{"ok": 1}]}
    decision = ledger.decisions[-1]
    assert decision.record_refs == ("one",)
    assert decision.stripped_fields == ("person_name", "Phone_Number")
    assert (decision.records_checked, decision.records_dropped) == (1, 0)


def test_unlisted_permit_mode_lets_tools_run(policy: ScopePolicy) -> None:
    copy = policy.model_dump()
    copy["defaults"]["unlisted_tools"] = "permit"
    ledger = create_scope_ledger()
    gate = create_data_scope_gate(ScopePolicy.model_validate(copy), LESSON_AGENT, context={"incident": {"id": LESSON_INCIDENT}}, on_decision=ledger.record)
    ran = []
    result = gate.wrap_tool_call(_Request("anything"), lambda req: (ran.append(1), ToolMessage(content="{}", tool_call_id="call-1", name="anything"))[1])
    assert ran and result.content == "{}"
    assert (ledger.decisions[-1].decision, ledger.decisions[-1].rule) == ("permit", "defaults.unlisted_tools")
    assert ledger.summary().highest_tier is None
    assert ledger.mark() == "completed within scope"


def test_stop_mode_raises_before_the_tool_runs(policy: ScopePolicy) -> None:
    ledger = create_scope_ledger(policy)
    gate = _gate(policy, ledger, on_block="stop")
    ran = []
    with pytest.raises(DataScopeStopError) as caught:
        gate.wrap_tool_call(_Request("get_household_records"), lambda req: ran.append(1))
    assert not ran
    assert caught.value.gate_decision.tool == "get_household_records"


def test_strip_forbidden_reports_names() -> None:
    removed: list[str] = []
    out = strip_forbidden({"a": 1, "email_address": "x", "list": [{"EmailAddress": "y"}, 3]}, frozenset({"emailaddress"}), removed)
    assert out == {"a": 1, "list": [{}, 3]}
    assert removed == ["email_address", "EmailAddress"]


def test_decision_to_dict_omits_absent_fields() -> None:
    decision = GateDecision(at="t", policy_version=1, agent="a", tool="t", decision="block", rule="r")
    assert decision.to_dict() == {"at": "t", "policy_version": 1, "agent": "a", "tool": "t", "decision": "block", "rule": "r"}


def test_async_invoke_runs_the_same_gate(policy_path: str) -> None:
    """Only the sync hook would make ``ainvoke`` raise NotImplementedError; both are implemented."""
    import asyncio

    session = create_lesson_agent(scripted_lesson_model(), on_block="continue", policy_path=policy_path)
    tracer = CollectingTracer()
    result = asyncio.run(session.agent.ainvoke({"messages": [{"role": "user", "content": "go"}]}, {"callbacks": [tracer], "recursion_limit": 8}))
    tools = {m.name: m for m in result["messages"] if isinstance(m, ToolMessage)}
    assert tools["get_household_records"].status == "error"
    assert "example.invalid" not in str(tools["get_incident_snapshot"].content)
    assert session.ledger.mark() == "completed with 1 blocked read, 1 record dropped, 1 field stripped"
    assert session.ran["forbidden_tool"] == 0
    assert any(run.name.startswith(f"DataScopeGate summary {LESSON_AGENT}") for run in tracer.runs)
