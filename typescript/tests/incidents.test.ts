import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadScopePolicy, type GateDecision } from "../src/gate.js";
import { buildFleetReport, renderFleetReport } from "../src/fleet-report.js";
import { detectRunIncidents, incidentFeedback, openIncidents, readIncidents, transitionIncident } from "../src/incidents.js";
import { createDataScopeLessonAgent, runDataScopeLesson, scriptedLessonModel, LESSON_AGENT, LESSON_SCI_TOOL } from "../src/lesson.js";

globalThis.fetch = async () => { throw new Error("Offline only"); };
const policy = loadScopePolicy("../policies/example-scope.yaml");
assert.deepEqual(policy.incidents, { owner: "platform-team", sensitive_tiers: ["SCI"], necessity_floor: 0.25, necessity_streak: 3, feedback_key: "data_scope_incident_v1" });

const at = "2026-09-06T21:00:00.000Z";
const decision = (partial: Partial<GateDecision> & Pick<GateDecision, "tool" | "decision">): GateDecision =>
  ({ at, policyVersion: 1, agent: "example-support-drafter", rule: "agents.example-support-drafter.permit", ...partial });
const base = { runId: "44444444-4444-4444-8444-444444444444", runName: "Support draft", startedAt: at, integrity: "ok" as const, necessity: 0.5, priorNecessity: [] as number[] };

// Rule 1: a strip on an SCI source is an incident; the same strip on BCI is a statistic.
{
  const sci = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI", strippedFields: ["phone_number"] })] }, policy);
  assert.equal(sci.length, 1);
  assert.equal(sci[0].type, "sensitive_gate_action");
  assert.equal(sci[0].owner, "platform-team");
  assert.match(sci[0].detail, /stripped phone_number/);
  const bci = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incident_snapshot", decision: "permit", tier: "BCI", strippedFields: ["phone_number"] })] }, policy);
  assert.equal(bci.length, 0);
  const cleanSci = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI", recordsChecked: 1, recordsDropped: 0 })] }, policy);
  assert.equal(cleanSci.length, 0, "a clean read of a sensitive source is not an incident");
  const blockedSci = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incoming_message", decision: "block", tier: "SCI", rule: "agents.example-support-drafter.permit.scope" })] }, policy);
  assert.equal(blockedSci[0]?.type, "sensitive_gate_action");
}
// Rule 2: integrity.
{
  const flagged = detectRunIncidents({ ...base, integrity: "cited_not_fetched", decisions: [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI" })] }, policy);
  assert.deepEqual(flagged.map((incident) => incident.type), ["integrity"]);
}
// Rule 3: necessity streak needs the full window below the floor.
{
  const decisions = [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI" })];
  assert.equal(detectRunIncidents({ ...base, decisions, necessity: 0.1, priorNecessity: [0.2, 0.1] }, policy).filter((i) => i.type === "necessity_streak").length, 1);
  assert.equal(detectRunIncidents({ ...base, decisions, necessity: 0.1, priorNecessity: [0.9, 0.1] }, policy).filter((i) => i.type === "necessity_streak").length, 0, "one good run breaks the streak");
  assert.equal(detectRunIncidents({ ...base, decisions, necessity: 0.1, priorNecessity: [0.1] }, policy).filter((i) => i.type === "necessity_streak").length, 0, "not enough runs yet");
  assert.equal(detectRunIncidents({ ...base, decisions, necessity: 0.1, priorNecessity: [null, 0.1] }, policy).filter((i) => i.type === "necessity_streak").length, 0, "an unscored run does not count as low");
}
// Feedback for LangSmith alerts: 1 when an incident opened, names only.
{
  const incidents = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI", strippedFields: ["message_text"] })] }, policy);
  const feedback = incidentFeedback(incidents, policy);
  assert.equal(feedback.key, "data_scope_incident_v1");
  assert.equal(feedback.score, 1);
  assert.match(feedback.comment, /sensitive_gate_action \[example-support-drafter, SCI\] permitted get_incoming_message, stripped message_text/);
  assert.equal(incidentFeedback([], policy).score, 0);
}
// The record: open, dedupe, move forward only, show on the fleet report.
const dir = mkdtempSync(join(tmpdir(), "scope-incidents-"));
try {
  const path = join(dir, "incidents.jsonl");
  const incidents = detectRunIncidents({ ...base, decisions: [decision({ tool: "get_incoming_message", decision: "permit", tier: "SCI", strippedFields: ["message_text"] })] }, policy);
  assert.equal(openIncidents(path, incidents, at).length, 1);
  assert.equal(openIncidents(path, incidents, at).length, 0, "already on file");
  const id = incidents[0].id;
  assert.equal(readIncidents(path)[0].state, "open");
  transitionIncident(path, id, "acknowledged", "looking into the tool", at);
  assert.throws(() => transitionIncident(path, id, "open", undefined, at), /already acknowledged/);
  transitionIncident(path, id, "closed", undefined, at);
  const record = readIncidents(path)[0];
  assert.equal(record.state, "closed");
  assert.deepEqual(record.history.map((entry) => entry.state), ["open", "acknowledged", "closed"]);
  assert.equal(record.history[1].note, "looking into the tool");
  const report = renderFleetReport(buildFleetReport([], "test", { since: new Date(0), until: new Date(at) }, policy, readIncidents(path)));
  assert.match(report, /Open 0, acknowledged 0, remediated 0, closed 1/);
  assert.match(report, /\| closed \| sensitive_gate_action \| `example-support-drafter` \| SCI \| 2026-09-06 21:00 \| platform-team \| permitted get_incoming_message, stripped message_text \|/);
  assert.ok(!report.includes("must never reach"), "no data on the report");
} finally { rmSync(dir, { recursive: true, force: true }); }

// End to end on the lesson agent: a strip on a lesson-only SCI source becomes an incident, and the model never saw the field.
{
  const session = createDataScopeLessonAgent(scriptedLessonModel({ sciProbe: true }), { onBlock: "continue", sciProbe: true });
  const outcome = await runDataScopeLesson(session);
  assert.equal(outcome.ran.sciTool, 1);
  const sci = outcome.decisions.find((d) => d.tool === LESSON_SCI_TOOL)!;
  assert.deepEqual([sci.decision, sci.tier, sci.strippedFields], ["permit", "SCI", ["message_text"]]);
  const seen = outcome.toolMessages.find((message) => message.name === LESSON_SCI_TOOL)!;
  assert.ok(!String(seen.content).includes("must never reach"), "the forbidden field never reached the model");
  assert.equal(outcome.summary.highestTier, "SCI");
  const incidents = detectRunIncidents({ runId: "55555555-5555-4555-8555-555555555555", runName: "lesson", startedAt: at, decisions: outcome.decisions, necessity: outcome.necessity.overall.necessity, integrity: outcome.necessity.overall.integrity, priorNecessity: [] }, session.policy);
  assert.deepEqual(incidents.map((incident) => [incident.type, incident.agent, incident.tier]), [["sensitive_gate_action", LESSON_AGENT, "SCI"]]);
}
console.log("Incidents passed: three deterministic rules, owner from policy, append-only record with forward-only states, feedback key for LangSmith alerts, fleet report section.");
