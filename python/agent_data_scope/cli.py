"""Command line: ``agent-data-scope card|lesson|report|incident``.

  card [--check]                     write (or check) the policy card
  lesson [--sci-probe]               run the fictional lesson; traces and posts
                                     feedback when LANGSMITH_API_KEY is set
  report --source langsmith|ledger   the fleet report over a period
  incident [--id --state --note]     list incidents, or move one forward

Reads only, apart from the artifacts it writes. No model is called or paid for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .fleet_report import AgentSummary, FleetRunRecord, Period, append_fleet_run_record, build_fleet_report, langsmith_decision_source, ledger_decision_source, render_fleet_report
from .gate import now_iso
from .incidents import INCIDENT_STATES, RunForIncidents, detect_run_incidents, open_incidents, read_incidents, record_incident_feedback, transition_incident
from .lesson import LESSON_AGENT, LESSON_POLICY_PATH, create_lesson_agent, run_lesson, scripted_lesson_model
from .necessity import record_necessity_feedback
from .policy import load_scope_policy
from .policy_card import policy_card_is_current, render_policy_card

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_POLICY = LESSON_POLICY_PATH
DEFAULT_CARD = os.path.join(PYTHON_DIR, "example-scope.md")
"""The Python port's card lives beside the package so it does not collide with the TypeScript port's."""
DEFAULT_LEDGER = os.path.join(PYTHON_DIR, "artifacts", "data-scope-ledger.jsonl")
DEFAULT_INCIDENTS = os.path.join(PYTHON_DIR, "artifacts", "data-scope-incidents.jsonl")
DEFAULT_PROJECT = "agent-data-scope-lesson"
LESSON_RUN_NAME = "Data scope gate lesson: scope, forbid, on_block continue"


def _card(args: argparse.Namespace) -> int:
    policy = load_scope_policy(args.policy)
    label = os.path.relpath(args.policy, os.path.dirname(PYTHON_DIR)) if os.path.isabs(args.policy) else args.policy
    if args.check:
        if not policy_card_is_current(policy, args.out, policy_label=label):
            print(f"{args.out} is out of date. Run agent-data-scope card.", file=sys.stderr)
            return 1
        print("Policy card matches the policy file.")
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render_policy_card(policy, policy_label=label))
    print(f"Wrote {args.out} for policy version {policy.version}.")
    return 0


def _lesson(args: argparse.Namespace) -> int:
    key = os.environ.get("LANGSMITH_API_KEY")
    run_id = str(uuid.uuid4())
    started_at = now_iso()
    session = create_lesson_agent(scripted_lesson_model(sci_probe=args.sci_probe), on_block="continue", policy_path=args.policy, sci_probe=args.sci_probe)

    client: Any = None
    config: dict[str, Any] = {"run_id": run_id, "run_name": LESSON_RUN_NAME, "tags": ["lesson", "data-scope-gate"]}
    if key:
        from langchain_core.tracers.langchain import LangChainTracer
        from langsmith import Client

        client = Client(api_key=key)
        config["callbacks"] = [LangChainTracer(project_name=args.project, client=client)]
    outcome = run_lesson(session, config)

    feedback: Any = "not recorded (LANGSMITH_API_KEY not set)"
    incident_feedback_result: Any = feedback
    if client is not None:
        client.flush()
        try:
            feedback = record_necessity_feedback(client, run_id, args.project, outcome.necessity)
        except Exception as error:  # noqa: BLE001 - report, do not hide
            feedback = f"not recorded: {error}"

    # Incidents: detected from this run plus the ledger's earlier necessity scores, opened on file, posted as feedback.
    prior = [record.necessity for record in reversed(ledger_decision_source(args.ledger).runs(Period(since=datetime.fromtimestamp(0, timezone.utc), until=datetime.now(timezone.utc))))]
    run = RunForIncidents(run_id=run_id, run_name="Data scope gate lesson", started_at=started_at, decisions=outcome.decisions, necessity=outcome.necessity.overall.necessity, integrity=outcome.necessity.overall.integrity, prior_necessity=prior)
    incidents = detect_run_incidents(run, session.policy)
    opened = open_incidents(args.incidents, incidents)
    if client is not None:
        try:
            incident_feedback_result = record_incident_feedback(client, run_id, args.project, incidents, session.policy)
        except Exception as error:  # noqa: BLE001
            incident_feedback_result = f"not recorded: {error}"

    # The same record the LangSmith source reconstructs, kept locally for teams that prefer their own store.
    summary = outcome.summary
    append_fleet_run_record(
        args.ledger,
        FleetRunRecord(
            run_id=run_id,
            run_name=LESSON_RUN_NAME,
            started_at=started_at,
            policy_version=outcome.decisions[0].policy_version if outcome.decisions else None,
            agents=(AgentSummary(agent=LESSON_AGENT, permitted=summary.permitted, blocked=summary.blocked, dropped=summary.records_dropped, stripped=summary.fields_stripped, highest_tier=summary.highest_tier),),
            necessity=outcome.necessity.overall.necessity,
            integrity=outcome.necessity.overall.integrity,
        ),
    )

    print(
        json.dumps(
            {
                "project": args.project if key else None,
                "trace_id": run_id,
                "traced": bool(key),
                "incidents_opened": [incident.to_json() for incident in opened],
                "incident_feedback": incident_feedback_result,
                "mark": outcome.mark,
                "summary": summary.__dict__,
                "necessity": outcome.necessity.to_dict(),
                "feedback": feedback,
                "decisions": [decision.to_dict() for decision in outcome.decisions],
                "ran": outcome.ran,
                "ledger": args.ledger,
                "incidents_file": args.incidents,
            },
            indent=2,
        )
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    policy = load_scope_policy(args.policy)
    if args.source == "ledger":
        source: Any = ledger_decision_source(args.ledger)
    else:
        key = os.environ.get("LANGSMITH_API_KEY")
        if not key:
            print("LANGSMITH_API_KEY is not set. Use --source ledger or export the key.", file=sys.stderr)
            return 1
        from langsmith import Client

        source = langsmith_decision_source(Client(api_key=key), args.projects.split(","))
    period = Period(since=since, until=until)
    records = source.runs(period)
    incidents = read_incidents(args.incidents)
    markdown = render_fleet_report(build_fleet_report(records, source.name, period, policy, incidents))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"Wrote {args.out} ({len(records)} runs).")
    else:
        print(markdown)
    return 0


def _incident(args: argparse.Namespace) -> int:
    if not args.id:
        for incident in read_incidents(args.path):
            print(f"{incident.state:<12} {incident.type:<22} {incident.agent:<40} owner {incident.owner:<10} {incident.id}")
        return 0
    if not args.state or args.state not in INCIDENT_STATES:
        print(f"--state must be one of {', '.join(INCIDENT_STATES)}", file=sys.stderr)
        return 1
    updated = transition_incident(args.path, args.id, args.state, args.note)
    print(f"{updated.id} -> {updated.state}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-data-scope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    card = commands.add_parser("card", help="write the policy card from the policy file, or --check that it is current")
    card.add_argument("--check", action="store_true")
    card.add_argument("--policy", default=DEFAULT_POLICY)
    card.add_argument("--out", default=DEFAULT_CARD)
    card.set_defaults(run=_card)

    lesson = commands.add_parser("lesson", help="run the fictional lesson (no model is paid for)")
    lesson.add_argument("--sci-probe", action="store_true", help="add a lesson-only SCI source so a strip becomes an incident")
    lesson.add_argument("--policy", default=DEFAULT_POLICY)
    lesson.add_argument("--ledger", default=DEFAULT_LEDGER)
    lesson.add_argument("--incidents", default=DEFAULT_INCIDENTS)
    lesson.add_argument("--project", default=DEFAULT_PROJECT, help="LangSmith project, used only when LANGSMITH_API_KEY is set")
    lesson.set_defaults(run=_lesson)

    report = commands.add_parser("report", help="the fleet report over a period")
    report.add_argument("--source", choices=["langsmith", "ledger"], default="langsmith")
    report.add_argument("--projects", default=DEFAULT_PROJECT, help="comma-separated LangSmith projects")
    report.add_argument("--ledger", default=DEFAULT_LEDGER)
    report.add_argument("--incidents", default=DEFAULT_INCIDENTS)
    report.add_argument("--policy", default=DEFAULT_POLICY)
    report.add_argument("--days", type=int, default=7)
    report.add_argument("--out")
    report.set_defaults(run=_report)

    incident = commands.add_parser("incident", help="list incidents, or move one forward with --id and --state")
    incident.add_argument("--id")
    incident.add_argument("--state", choices=list(INCIDENT_STATES))
    incident.add_argument("--note")
    incident.add_argument("--path", default=DEFAULT_INCIDENTS)
    incident.set_defaults(run=_incident)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
