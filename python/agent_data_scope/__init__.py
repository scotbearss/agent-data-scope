"""Agent data scope: a governance layer for LangChain Deep Agents that proves an
agent touched only the data its task needed.

Four parts: a YAML policy (permit / forbid / expect, sensitivity tiers, approval,
incident rules); a middleware gate on every tool call; deterministic necessity
scoring posted as LangSmith feedback; and incidents with an owner and a state,
a fleet report, and a generated policy card.
"""

from .fleet_report import (
    AgentSummary,
    FleetReport,
    FleetRunRecord,
    Period,
    append_fleet_run_record,
    build_fleet_report,
    integrity_from_comment,
    langsmith_decision_source,
    ledger_decision_source,
    parse_summary_step,
    policy_version_from_tags,
    render_fleet_report,
)
from .gate import DataScopeGate, DataScopeStopError, GateDecision, LedgerSummary, ScopeLedger, create_data_scope_gate, create_scope_ledger
from .incidents import (
    INCIDENT_STATES,
    Incident,
    IncidentRecord,
    RunForIncidents,
    detect_run_incidents,
    fold_incidents,
    incident_feedback,
    open_incidents,
    read_incident_events,
    read_incidents,
    record_incident_feedback,
    transition_incident,
)
from .necessity import NecessityScore, necessity_feedback, record_necessity_feedback, score_necessity, wait_for_run
from .policy import ScopePolicy, highest_tier, load_scope_policy, parse_scope_policy, tier_rank
from .policy_card import policy_card_is_current, render_policy_card

__all__ = [
    "AgentSummary",
    "DataScopeGate",
    "DataScopeStopError",
    "FleetReport",
    "FleetRunRecord",
    "GateDecision",
    "INCIDENT_STATES",
    "Incident",
    "IncidentRecord",
    "LedgerSummary",
    "NecessityScore",
    "Period",
    "RunForIncidents",
    "ScopeLedger",
    "ScopePolicy",
    "append_fleet_run_record",
    "build_fleet_report",
    "create_data_scope_gate",
    "create_scope_ledger",
    "detect_run_incidents",
    "fold_incidents",
    "highest_tier",
    "incident_feedback",
    "integrity_from_comment",
    "langsmith_decision_source",
    "ledger_decision_source",
    "load_scope_policy",
    "necessity_feedback",
    "open_incidents",
    "parse_scope_policy",
    "parse_summary_step",
    "policy_card_is_current",
    "policy_version_from_tags",
    "read_incident_events",
    "read_incidents",
    "record_incident_feedback",
    "record_necessity_feedback",
    "render_fleet_report",
    "render_policy_card",
    "score_necessity",
    "tier_rank",
    "transition_incident",
    "wait_for_run",
]
