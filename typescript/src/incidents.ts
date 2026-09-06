import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Client } from "langsmith";
import type { GateDecision, ScopePolicy } from "./gate.js";
import { waitForRun } from "./necessity.js";

/**
 * Incidents, step six part three. Three deterministic rules turn a gate event
 * into something with an owner and a state, instead of a statistic:
 *   sensitive_gate_action  the gate blocked, dropped or stripped on a source
 *                          whose tier is in incidents.sensitive_tiers. Nothing
 *                          reached the model; something upstream drifted.
 *   integrity              a report cited evidence the gate never let through.
 *   necessity_streak       necessity below the floor for N runs in a row.
 * Incidents are recorded locally as an append-only event log (open, then
 * state changes) and posted to LangSmith as feedback so a LangSmith alert can
 * notify people. Records carry names, counts and identifiers, never data.
 */

export type IncidentType = "sensitive_gate_action" | "integrity" | "necessity_streak";
export type IncidentState = "open" | "acknowledged" | "remediated" | "closed";
export const INCIDENT_STATES: readonly IncidentState[] = ["open", "acknowledged", "remediated", "closed"];

export type Incident = Readonly<{
  id: string;
  type: IncidentType;
  runId: string;
  runName: string;
  startedAt: string;
  agent: string;
  tier?: string;
  detail: string;
  owner: string;
  policyVersion: number;
}>;

export type IncidentEvent = Readonly<
  | { kind: "opened"; at: string; incident: Incident }
  | { kind: "state"; at: string; id: string; state: IncidentState; note?: string }
>;

export type IncidentRecord = Incident & Readonly<{ state: IncidentState; history: readonly { at: string; state: IncidentState; note?: string }[] }>;

export type RunForIncidents = Readonly<{
  runId: string;
  runName: string;
  startedAt: string;
  decisions: readonly GateDecision[];
  necessity: number | null;
  integrity: "ok" | "cited_not_fetched" | "unknown";
  /** Necessity of earlier runs, oldest first. Used for the streak rule. */
  priorNecessity: readonly (number | null)[];
}>;

export function detectRunIncidents(run: RunForIncidents, policy: ScopePolicy): Incident[] {
  const rules = policy.incidents;
  if (!rules) return [];
  const owner = (agent: string) => policy.agents[agent]?.owner ?? rules.owner;
  const sensitive = new Set(rules.sensitive_tiers);
  const incidents: Incident[] = [];
  const base = { runId: run.runId, runName: run.runName, startedAt: run.startedAt, policyVersion: run.decisions[0]?.policyVersion ?? policy.version };

  for (const decision of run.decisions) {
    if (!decision.tier || !sensitive.has(decision.tier)) continue;
    const acted = decision.decision === "block" || (decision.recordsDropped ?? 0) > 0 || (decision.strippedFields?.length ?? 0) > 0;
    if (!acted) continue;
    const what = decision.decision === "block" ? `blocked ${decision.tool} (${decision.rule})`
      : [`permitted ${decision.tool}`, decision.recordsDropped ? `dropped ${decision.recordsDropped}` : "", decision.strippedFields?.length ? `stripped ${decision.strippedFields.join(",")}` : ""].filter(Boolean).join(", ");
    incidents.push({ ...base, id: `sensitive_gate_action:${run.runId}:${decision.agent}:${decision.tool}`, type: "sensitive_gate_action", agent: decision.agent, tier: decision.tier, detail: what, owner: owner(decision.agent) });
  }

  if (run.integrity === "cited_not_fetched") {
    const agent = run.decisions[0]?.agent ?? "unknown";
    incidents.push({ ...base, id: `integrity:${run.runId}`, type: "integrity", agent, detail: "a report cited evidence the gate did not let through", owner: owner(agent) });
  }

  const window = [...run.priorNecessity.slice(-(rules.necessity_streak - 1)), run.necessity];
  if (window.length === rules.necessity_streak && window.every((score) => score !== null && score < rules.necessity_floor)) {
    const agent = run.decisions[0]?.agent ?? "unknown";
    incidents.push({ ...base, id: `necessity_streak:${run.runId}`, type: "necessity_streak", agent, detail: `necessity below ${rules.necessity_floor} for ${rules.necessity_streak} runs in a row (${window.map((score) => (score ?? 0).toFixed(2)).join(", ")})`, owner: owner(agent) });
  }
  return incidents;
}

// ---- the record: an append-only event log, folded on read ---------------

export function readIncidentEvents(path: string): IncidentEvent[] {
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line) as IncidentEvent);
}

export function foldIncidents(events: readonly IncidentEvent[]): IncidentRecord[] {
  const records = new Map<string, IncidentRecord>();
  for (const event of events) {
    if (event.kind === "opened") {
      if (!records.has(event.incident.id)) records.set(event.incident.id, { ...event.incident, state: "open", history: [{ at: event.at, state: "open" }] });
    } else {
      const current = records.get(event.id);
      if (current) records.set(event.id, { ...current, state: event.state, history: [...current.history, { at: event.at, state: event.state, ...(event.note ? { note: event.note } : {}) }] });
    }
  }
  return [...records.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
}

export function readIncidents(path: string) { return foldIncidents(readIncidentEvents(path)); }

/** Opens incidents that are not already on file. Returns the ones newly opened. */
export function openIncidents(path: string, incidents: readonly Incident[], at = new Date().toISOString()) {
  const existing = new Set(readIncidents(path).map((incident) => incident.id));
  const opened: Incident[] = [];
  for (const incident of incidents) {
    if (existing.has(incident.id)) continue;
    mkdirSync(dirname(path), { recursive: true });
    appendFileSync(path, `${JSON.stringify({ kind: "opened", at, incident } satisfies IncidentEvent)}\n`);
    opened.push(incident);
  }
  return opened;
}

export function transitionIncident(path: string, id: string, state: IncidentState, note?: string, at = new Date().toISOString()) {
  const current = readIncidents(path).find((incident) => incident.id === id);
  if (!current) throw new Error(`No incident with id ${id}`);
  const order = INCIDENT_STATES.indexOf(state);
  if (order <= INCIDENT_STATES.indexOf(current.state)) throw new Error(`Incident ${id} is already ${current.state}; cannot move to ${state}`);
  appendFileSync(path, `${JSON.stringify({ kind: "state", at, id, state, ...(note ? { note } : {}) } satisfies IncidentEvent)}\n`);
  return { ...current, state };
}

// ---- LangSmith feedback, the signal a LangSmith alert watches -------------

export function incidentFeedback(incidents: readonly Incident[], policy: ScopePolicy) {
  const key = policy.incidents?.feedback_key ?? "data_scope_incident_v1";
  return {
    key,
    score: incidents.length > 0 ? 1 : 0,
    comment: incidents.length === 0
      ? "No data scope incident on this run."
      : `${incidents.length} data scope incident${incidents.length === 1 ? "" : "s"}: ${incidents.map((incident) => `${incident.type} [${incident.agent}${incident.tier ? `, ${incident.tier}` : ""}] ${incident.detail}`).join("; ")}. Names and counts only.`,
  };
}

export async function recordIncidentFeedback(client: Client, runId: string, projectName: string, incidents: readonly Incident[], policy: ScopePolicy) {
  const project = await client.readProject({ projectName });
  const waitedMs = await waitForRun(client, runId, project.id, 60_000);
  const feedback = incidentFeedback(incidents, policy);
  await client.createFeedback({ runId, sessionId: project.id, key: feedback.key, score: feedback.score, comment: feedback.comment, feedbackSourceType: "api", extendTraceRetention: false });
  return { ...feedback, waitedMs };
}
