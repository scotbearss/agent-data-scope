import { randomUUID } from "node:crypto";
import type { RunnableConfig } from "@langchain/core/runnables";
import { createDataScopeLessonAgent, runDataScopeLesson, scriptedLessonModel, LESSON_AGENT } from "./lesson.js";
import { appendFleetRunRecord, ledgerDecisionSource } from "./fleet-report.js";
import { detectRunIncidents, openIncidents, recordIncidentFeedback } from "./incidents.js";
import { recordNecessityFeedback } from "./necessity.js";
import { createTracer, traceClient } from "./tracing.js";

/** Runs the fictional lesson with LangSmith tracing. No model is called or paid for. */
const key = process.env.LANGSMITH_API_KEY;   // optional: without it the lesson runs offline and records locally only
const PROJECT = process.env.LANGSMITH_PROJECT ?? "agent-data-scope-lesson";
const client = key ? traceClient(key) : undefined;
const tracer = client ? createTracer(client, PROJECT) : undefined;
const runId = randomUUID();
const startedAt = new Date().toISOString();

const LEDGER = "runs/ledger.jsonl";
const INCIDENTS = "runs/incidents.jsonl";
const sciProbe = process.argv.includes("--sci-probe");
const session = createDataScopeLessonAgent(scriptedLessonModel({ sciProbe }), { onBlock: "continue", sciProbe });
const outcome = await runDataScopeLesson(session, {
  ...(tracer ? { callbacks: [tracer] as RunnableConfig["callbacks"] } : {}),
  runId,
  runName: "Data scope gate lesson: scope, forbid, on_block continue",
  tags: ["lesson", "data-scope-gate"],
});
if (client) await client.awaitPendingTraceBatches();
let feedback: unknown = client ? "not recorded" : "skipped: no LANGSMITH_API_KEY";
if (client) { try { feedback = await recordNecessityFeedback(client, runId, PROJECT, outcome.necessity); } catch (error) { feedback = `not recorded: ${error instanceof Error ? error.message : String(error)}`; } }

// Incidents: detected from this run plus the ledger's earlier necessity scores, opened on file, posted as feedback.
const prior = (await ledgerDecisionSource(LEDGER).runs({ since: new Date(0), until: new Date() })).reverse().map((record) => record.necessity);
const incidents = detectRunIncidents({ runId, runName: "Data scope gate lesson", startedAt, decisions: outcome.decisions, necessity: outcome.necessity.overall.necessity, integrity: outcome.necessity.overall.integrity, priorNecessity: prior }, session.policy);
const opened = openIncidents(INCIDENTS, incidents);
let incidentFeedback: unknown = client ? "not recorded" : "skipped: no LANGSMITH_API_KEY";
if (client) { try { incidentFeedback = await recordIncidentFeedback(client, runId, PROJECT, incidents, session.policy); } catch (error) { incidentFeedback = `not recorded: ${error instanceof Error ? error.message : String(error)}`; } }

// The same record the LangSmith source reconstructs, kept locally for teams that prefer their own store.
appendFleetRunRecord(LEDGER, {
  runId, runName: "Data scope gate lesson: scope, forbid, on_block continue", startedAt,
  policyVersion: outcome.decisions[0]?.policyVersion ?? null,
  agents: [{ agent: LESSON_AGENT, permitted: outcome.summary.permitted, blocked: outcome.summary.blocked, dropped: outcome.summary.recordsDropped, stripped: outcome.summary.fieldsStripped, ...(outcome.summary.highestTier ? { highestTier: outcome.summary.highestTier } : {}) }],
  necessity: outcome.necessity.overall.necessity,
  integrity: outcome.necessity.overall.integrity,
});

console.log(JSON.stringify({ project: PROJECT, traceId: runId, incidentsOpened: opened, incidentFeedback, mark: outcome.mark, summary: outcome.summary, necessity: outcome.necessity, feedback, decisions: outcome.decisions, ran: outcome.ran }, null, 2));
