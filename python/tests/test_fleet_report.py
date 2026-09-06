"""Port of data-scope-fleet-report.test.ts: summary steps parse, totals and
per-agent rows aggregate, ledger source round-trips, no data in the report."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from agent_data_scope.fleet_report import AgentSummary, FleetRunRecord, Period, append_fleet_run_record, build_fleet_report, integrity_from_comment, ledger_decision_source, parse_summary_step, policy_version_from_tags, render_fleet_report
from agent_data_scope.policy import ScopePolicy


def test_summary_step_names_are_the_wire_format() -> None:
    assert parse_summary_step("DataScopeGate summary example-investigator | permitted 1 | blocked 1 | dropped 1 | stripped 1 | highest tier BCI") == AgentSummary(
        agent="example-investigator", permitted=1, blocked=1, dropped=1, stripped=1, highest_tier="BCI"
    )
    assert parse_summary_step("DataScopeGate summary lead | permitted 2 | blocked 0 | dropped 0 | stripped 0") == AgentSummary(agent="lead", permitted=2, blocked=0, dropped=0, stripped=0)
    assert parse_summary_step("DataScopeGate permit get_incident_snapshot | dropped 1") is None
    assert policy_version_from_tags(["lesson", "policy-v3", "tier-BCI"]) == 3
    assert policy_version_from_tags(["lesson"]) is None
    assert policy_version_from_tags(None) is None
    assert integrity_from_comment("fetched 2, used 1, unused 1, integrity ok.") == "ok"
    assert integrity_from_comment("integrity cited_not_fetched.") == "cited_not_fetched"
    assert integrity_from_comment(None) == "unknown"


def _clean(run_id: str, started_at: str) -> FleetRunRecord:
    agents = ["example-evidence-verifier", "example-investigator", "example-release-investigator", "example-lead"]
    return FleetRunRecord(
        run_id=run_id,
        run_name="Example investigation",
        started_at=started_at,
        policy_version=3,
        necessity=0.6,
        integrity="ok",
        agents=tuple(AgentSummary(agent=agent, permitted=2 if "release" in agent else 1, blocked=0, dropped=0, stripped=0, highest_tier="BCI") for agent in agents),
    )


ACTED = FleetRunRecord(
    run_id="33333333-3333-4333-8333-333333333333",
    run_name="Data scope gate lesson",
    started_at="2026-09-06T20:00:00.000Z",
    policy_version=3,
    necessity=0.5,
    integrity="ok",
    agents=(AgentSummary(agent="example-investigator", permitted=1, blocked=1, dropped=1, stripped=1, highest_tier="BCI"),),
)
RECORDS = [_clean("11111111-1111-4111-8111-111111111111", "2026-09-05T10:00:00.000Z"), _clean("22222222-2222-4222-8222-222222222222", "2026-09-06T10:00:00.000Z"), ACTED]
PERIOD = Period(since=datetime(2026, 9, 1, tzinfo=timezone.utc), until=datetime(2026, 9, 7, tzinfo=timezone.utc))


def test_fleet_totals_and_rows(policy: ScopePolicy) -> None:
    report = build_fleet_report(RECORDS, "test", PERIOD, policy)
    assert report.policy_versions == (3,)
    assert report.totals.runs == 3
    assert report.totals.clean == 2
    assert report.totals.gate_acted == 1
    assert (report.totals.blocked, report.totals.dropped, report.totals.stripped) == (1, 1, 1)
    assert report.totals.necessity_scored == 3
    assert abs(report.totals.necessity_average - (0.6 + 0.6 + 0.5) / 3) < 1e-9
    assert report.totals.integrity_flags == 0
    assert report.totals.highest_tier == "BCI"
    platform = next(agent for agent in report.agents if agent.agent == "example-investigator")
    assert (platform.runs, platform.clean, platform.blocked) == (3, 2, 1)
    assert len(report.gate_acted_runs) == 1
    assert report.gate_acted_runs[0].run_id == ACTED.run_id
    assert report.period_since == "2026-09-01T00:00:00.000Z"

    markdown = render_fleet_report(report)
    assert re.search(r"\| 3 \| 2 \(67%\) \| 1 \(33%\) \| 1 \| 1 \| 1 \| 0\.57 over 3 \| 0 \| BCI \|", markdown)
    assert re.search(r"`example-investigator` \| 3 \| 2 \(67%\) \| 1 \| 1 \| 1 \| BCI", markdown)
    assert "Data scope gate lesson (`33333333`)" in markdown
    assert "fresh" not in markdown and "example.invalid" not in markdown, "the report carries counts, never data"
    assert "Open 0, acknowledged 0, remediated 0, closed 0" in markdown
    assert "None on file." in markdown


def test_report_without_policy_or_runs() -> None:
    report = build_fleet_report([], "empty", PERIOD)
    assert report.totals.necessity_average is None and report.totals.highest_tier is None
    markdown = render_fleet_report(report)
    assert "| 0 | 0 (n/a) | 0 (n/a) | 0 | 0 | 0 | n/a over 0 | 0 | n/a |" in markdown
    assert "None in this period." in markdown
    assert "Policy versions seen: none." in markdown
    # Without a policy the highest tier falls back to string order.
    assert build_fleet_report(RECORDS, "t", PERIOD).totals.highest_tier == "BCI"


def test_ledger_source_round_trips_and_honors_period(tmp_path) -> None:  # noqa: ANN001
    path = os.path.join(tmp_path, "nested", "ledger.jsonl")
    for record in RECORDS:
        append_fleet_run_record(path, record)
    # The file uses the TypeScript port's keys so both ports can read it.
    first = json.loads(open(path, encoding="utf-8").readline())
    assert set(first) == {"runId", "runName", "startedAt", "policyVersion", "agents", "necessity", "integrity"}
    assert set(first["agents"][0]) == {"agent", "permitted", "blocked", "dropped", "stripped", "highestTier"}

    source = ledger_decision_source(path)
    assert source.name == f"ledger:{path}"
    everything = source.runs(PERIOD)
    assert [record.run_id for record in everything] == [ACTED.run_id, RECORDS[1].run_id, RECORDS[0].run_id], "newest first"
    assert everything[0] == ACTED
    later = source.runs(Period(since=datetime(2026, 9, 6, tzinfo=timezone.utc), until=PERIOD.until))
    assert len(later) == 2
    naive = source.runs(Period(since=datetime(2026, 9, 6), until=datetime(2026, 9, 7)))
    assert len(naive) == 2, "naive datetimes are read as UTC"
    assert ledger_decision_source(os.path.join(tmp_path, "missing.jsonl")).runs(PERIOD) == []


def test_ledger_reads_a_typescript_written_record(tmp_path) -> None:  # noqa: ANN001
    path = os.path.join(tmp_path, "ts.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "runId": "abc",
                    "runName": "TS run",
                    "startedAt": "2026-09-06T12:00:00.000Z",
                    "policyVersion": 4,
                    "agents": [{"agent": "sentinel-real-platform-investigator", "permitted": 1, "blocked": 0, "dropped": 0, "stripped": 0}],
                    "necessity": None,
                    "integrity": "unknown",
                }
            )
            + "\n"
        )
    [record] = ledger_decision_source(path).runs(PERIOD)
    assert record.policy_version == 4 and record.agents[0].highest_tier is None and record.necessity is None
