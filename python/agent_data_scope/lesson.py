"""A tiny fictional agent for the data scope lesson. No model is paid for: the
"model" is a script that asks for two tools, one permitted and one not. The
permitted tool returns records from the right incident and one from another,
and one of them carries a forbidden field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from .gate import GateDecision, LedgerSummary, ScopeLedger, create_data_scope_gate, create_scope_ledger
from .necessity import NecessityScore, score_necessity
from .policy import OnBlock, ScopePolicy, load_scope_policy

LESSON_AGENT = "example-investigator"
LESSON_INCIDENT = "inc-fictional-0001"
LESSON_SCI_TOOL = "get_support_message_preview"
LESSON_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "policies", "example-scope.yaml")
"""``<repo>/policies/example-scope.yaml``, shared with the TypeScript port."""


class ScriptedChatModel(GenericFakeChatModel):
    """A fake chat model that plays back scripted AIMessages and accepts tool binding."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-lesson-model"


def scripted_lesson_model(sci_probe: bool = False) -> ScriptedChatModel:
    """The script the fake model follows: call the tools, then answer."""
    calls = [
        {"name": "get_incident_snapshot", "args": {}, "id": "lesson-read-1", "type": "tool_call"},
        {"name": "get_household_records", "args": {}, "id": "lesson-read-2", "type": "tool_call"},
    ]
    if sci_probe:
        calls.append({"name": LESSON_SCI_TOOL, "args": {}, "id": "lesson-read-3", "type": "tool_call"})
    final = json.dumps({"finding": "Fictional report: one delivery failure signal; household records were not available.", "evidenceRefs": ["snapshot:fictional:signals"]})

    def script() -> Iterator[AIMessage]:
        yield AIMessage(content="", tool_calls=calls)
        yield AIMessage(content=final)

    return ScriptedChatModel(messages=script())


@dataclass
class LessonSession:
    agent: Any
    policy: ScopePolicy
    ledger: ScopeLedger
    ran: dict[str, int] = field(default_factory=lambda: {"permitted_tool": 0, "forbidden_tool": 0, "sci_tool": 0})


def create_lesson_agent(model: Any, on_block: OnBlock | None = None, policy_path: str | None = None, sci_probe: bool = False) -> LessonSession:
    loaded = load_scope_policy(policy_path or LESSON_POLICY_PATH)
    # Lesson-only overrides, applied to a copy of the policy: the owner's on_block
    # choice, and (for the incident lesson) one extra permitted source at the
    # most sensitive tier, so a strip on it can be watched becoming an incident.
    copy = loaded.model_dump()
    block = copy["agents"][LESSON_AGENT]
    if on_block:
        block["on_block"] = on_block
    if sci_probe:
        block["permit"] = [*block["permit"], {"tool": LESSON_SCI_TOOL, "tier": "SCI"}]
    policy = ScopePolicy.model_validate(copy)
    ledger = create_scope_ledger(policy)
    ran = {"permitted_tool": 0, "forbidden_tool": 0, "sci_tool": 0}

    @tool
    def get_incident_snapshot() -> str:
        """Read fixed fictional incident signals and totals."""
        ran["permitted_tool"] += 1
        return json.dumps(
            {
                "version": 1,
                "records": [
                    {"ref": "snapshot:fictional:signals", "incidentId": LESSON_INCIDENT, "kind": "snapshot-signals", "value": {"fictional": True, "signals": ["delivery_failure"]}, "email_address": "fictional@example.invalid"},
                    {"ref": "snapshot:fictional:heartbeat", "incidentId": LESSON_INCIDENT, "kind": "snapshot-heartbeat", "value": {"fictional": True, "heartbeat": "fresh"}},
                    {"ref": "snapshot:fictional-other:platform", "incidentId": "inc-fictional-9999", "kind": "snapshot-platform", "value": {"fictional": True, "heartbeat": "stale"}},
                ],
            }
        )

    @tool
    def get_household_records() -> str:
        """A tool this agent is not permitted to use."""
        ran["forbidden_tool"] += 1
        return json.dumps({"fictional": True, "households": []})

    @tool(LESSON_SCI_TOOL)
    def get_support_message_preview() -> str:
        """Lesson only: a sensitive-tier source that returns a forbidden field."""
        ran["sci_tool"] += 1
        return json.dumps(
            {
                "version": 1,
                "records": [
                    {"ref": "message:fictional:1", "incidentId": LESSON_INCIDENT, "kind": "support-message", "subject": "fictional subject", "message_text": "fictional body that must never reach the model"},
                ],
            }
        )

    tools = [get_incident_snapshot, get_household_records] + ([get_support_message_preview] if sci_probe else [])
    agent = create_deep_agent(
        model=model,
        tools=tools,
        name=LESSON_AGENT,
        backend=StateBackend(),
        subagents=[],
        system_prompt="Fictional lesson agent. Read the incident snapshot, then report.",
        middleware=[create_data_scope_gate(policy, LESSON_AGENT, context={"incident": {"id": LESSON_INCIDENT}}, on_decision=ledger.record)],
    )
    return LessonSession(agent=agent, policy=policy, ledger=ledger, ran=ran)


@dataclass
class LessonOutcome:
    final_text: str
    cited_refs: list[str]
    necessity: NecessityScore
    tool_messages: list[ToolMessage]
    decisions: list[GateDecision]
    summary: LedgerSummary
    mark: str
    ran: dict[str, int]


def run_lesson(session: LessonSession, config: dict[str, Any] | None = None) -> LessonOutcome:
    """Runs the lesson. ``config`` may carry callbacks, run_id, run_name and tags for tracing."""
    result = session.agent.invoke({"messages": [{"role": "user", "content": "Run the fictional data scope lesson."}]}, {**(config or {}), "recursion_limit": 8})
    messages = result["messages"]
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    final_text = str(messages[-1].content) if messages else ""
    cited_refs: list[str] = []
    try:
        parsed = json.loads(final_text)
        refs = parsed.get("evidenceRefs") if isinstance(parsed, dict) else None
        if isinstance(refs, list):
            cited_refs = [ref for ref in refs if isinstance(ref, str)]
    except ValueError:
        pass  # a run that ends without a report cites nothing
    necessity = score_necessity(session.ledger.decisions, {LESSON_AGENT: cited_refs})
    return LessonOutcome(
        final_text=final_text,
        cited_refs=cited_refs,
        necessity=necessity,
        tool_messages=tool_messages,
        decisions=list(session.ledger.decisions),
        summary=session.ledger.summary(),
        mark=session.ledger.mark(),
        ran=session.ran,
    )
