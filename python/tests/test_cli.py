"""The CLI end to end, offline: card, lesson, report from the ledger, incident."""

from __future__ import annotations

import json
import os

import pytest

from agent_data_scope.cli import main
from agent_data_scope.lesson import LESSON_AGENT
from agent_data_scope.policy_card import render_policy_card


def test_card_writes_and_checks(tmp_path, capsys, policy, policy_path) -> None:  # noqa: ANN001
    card = os.path.join(tmp_path, "card.md")
    assert main(["card", "--check", "--policy", policy_path, "--out", card]) == 1
    assert "out of date" in capsys.readouterr().err
    assert main(["card", "--policy", policy_path, "--out", card]) == 0
    assert "Wrote" in capsys.readouterr().out
    text = open(card, encoding="utf-8").read()
    assert text == render_policy_card(policy, policy_label="policies/example-scope.yaml")
    assert text.startswith("# Data scope policy: example (version 2)")
    assert "Generated from `policies/example-scope.yaml`" in text
    assert "| Approval status | draft |" in text
    assert "| `example-support-drafter` | `get_incoming_message` | SCI | `message_id == case.message_id` | subject, body | stop |" in text
    assert "| `example-investigator` | `get_incident_snapshot` | BCI | `incident_id == incident.id` | all returned | stop |" in text
    assert "1. The investigator exists" in text
    assert "- `example-investigator`: records_read `relevant_to(incident.signals)`" in text
    assert "Default owner: **platform-team**. Per-agent owners: `example-investigator`: ops-team." in text
    assert "| 1 | 2026-09-06 | First example policy, extracted from the sentinel build. |" in text
    assert main(["card", "--check", "--policy", policy_path, "--out", card]) == 0
    assert "matches" in capsys.readouterr().out


def test_lesson_report_and_incident_offline(tmp_path, capsys, policy_path) -> None:  # noqa: ANN001
    ledger = os.path.join(tmp_path, "artifacts", "ledger.jsonl")
    incidents = os.path.join(tmp_path, "artifacts", "incidents.jsonl")
    common = ["--policy", policy_path, "--ledger", ledger, "--incidents", incidents]

    assert main(["lesson", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["traced"] is False and out["project"] is None
    assert out["mark"] == "completed with 1 blocked read, 1 record dropped, 1 field stripped"
    assert out["necessity"]["overall"]["necessity"] == 0.5
    assert out["incidents_opened"] == []
    assert out["feedback"].startswith("not recorded")
    assert [d["tool"] for d in out["decisions"]] and all("example.invalid" not in json.dumps(d) for d in out["decisions"])
    assert not os.path.exists(incidents), "nothing to open without the SCI probe"

    assert main(["lesson", "--sci-probe", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [i["type"] for i in out["incidents_opened"]] == ["sensitive_gate_action"]
    assert out["summary"]["highest_tier"] == "SCI"
    incident_id = out["incidents_opened"][0]["id"]

    lines = [json.loads(line) for line in open(ledger, encoding="utf-8") if line.strip()]
    assert len(lines) == 2 and lines[0]["agents"][0]["agent"] == LESSON_AGENT and lines[1]["agents"][0]["highestTier"] == "SCI"

    assert main(["report", "--source", "ledger", *common, "--days", "1"]) == 0
    markdown = capsys.readouterr().out
    assert "# Data scope fleet report" in markdown
    assert "| 2 | 0 (0%) | 2 (100%) |" in markdown
    assert "Open 1, acknowledged 0" in markdown
    report_path = os.path.join(tmp_path, "report.md")
    assert main(["report", "--source", "ledger", *common, "--out", report_path]) == 0
    assert "Wrote" in capsys.readouterr().out and os.path.exists(report_path)
    assert main(["report", "--source", "langsmith", *common]) == 1, "no key, no LangSmith"
    assert "LANGSMITH_API_KEY" in capsys.readouterr().err

    assert main(["incident", "--path", incidents]) == 0
    listing = capsys.readouterr().out
    assert listing.startswith("open") and incident_id in listing
    assert main(["incident", "--path", incidents, "--id", incident_id, "--state", "acknowledged", "--note", "looking"]) == 0
    assert capsys.readouterr().out.strip() == f"{incident_id} -> acknowledged"
    assert main(["incident", "--path", incidents, "--id", incident_id]) == 1
    with pytest.raises(ValueError, match="already acknowledged"):
        main(["incident", "--path", incidents, "--id", incident_id, "--state", "open"])
