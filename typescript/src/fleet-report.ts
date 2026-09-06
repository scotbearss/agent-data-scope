import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Client } from "langsmith";
import type { ScopePolicy } from "./gate.js";
import { highestTier } from "./gate.js";
import type { IncidentRecord } from "./incidents.js";

/**
 * Fleet report, step six part two. Governance does not read runs; it asks how
 * often the agents followed their rules over a period, across the fleet.
 *
 * The report is built from records that already exist: the gate's named trace
 * steps (tagged with policy version and tier) and the necessity feedback.
 * The default source is LangSmith, because that is where every other trace
 * already goes. A local ledger source exists for teams that prefer their own
 * store. Both produce the same record shape, so the report cannot tell them
 * apart, and neither carries data: names, counts, tiers and scores only.
 */

export type AgentSummary = Readonly<{
  agent: string;
  permitted: number;
  blocked: number;
  dropped: number;
  stripped: number;
  highestTier?: string;
}>;

export type FleetRunRecord = Readonly<{
  runId: string;
  runName: string;
  startedAt: string;
  policyVersion: number | null;
  agents: readonly AgentSummary[];
  necessity: number | null;
  integrity: "ok" | "cited_not_fetched" | "unknown";
}>;

export type Period = Readonly<{ since: Date; until: Date }>;

export interface DecisionSource {
  readonly name: string;
  runs(period: Period): Promise<FleetRunRecord[]>;
}

// ---- parsing the gate's trace step names ---------------------------------

const SUMMARY = /^DataScopeGate summary (\S+) \| permitted (\d+) \| blocked (\d+) \| dropped (\d+) \| stripped (\d+)(?: \| highest tier (\S+))?$/;

export function parseSummaryStep(name: string): AgentSummary | undefined {
  const match = SUMMARY.exec(name);
  if (!match) return undefined;
  return {
    agent: match[1], permitted: Number(match[2]), blocked: Number(match[3]), dropped: Number(match[4]), stripped: Number(match[5]),
    ...(match[6] ? { highestTier: match[6] } : {}),
  };
}

export function policyVersionFromTags(tags: readonly string[] | undefined) {
  for (const tag of tags ?? []) {
    const match = /^policy-v(\d+)$/.exec(tag);
    if (match) return Number(match[1]);
  }
  return null;
}

export function integrityFromComment(comment: string | null | undefined): FleetRunRecord["integrity"] {
  const match = /integrity (ok|cited_not_fetched)/.exec(comment ?? "");
  return match ? (match[1] as "ok" | "cited_not_fetched") : "unknown";
}

// ---- sources -------------------------------------------------------------

export function langSmithDecisionSource(client: Client, projectNames: readonly string[]): DecisionSource {
  return {
    name: `langsmith:${projectNames.join(",")}`,
    async runs(period) {
      const records: FleetRunRecord[] = [];
      for (const projectName of projectNames) {
        const project = await client.readProject({ projectName });
        const traces = client.traces.query({
          project_id: project.id,
          min_start_time: period.since.toISOString(),
          max_start_time: period.until.toISOString(),
          selects: ["ID", "NAME", "START_TIME", "TAGS"],
          page_size: 100,
        });
        for await (const trace of traces) {
          const root = trace.root_run;
          if (!root?.id) continue;
          const children = await client.traces.listRuns(root.id, {
            project_id: project.id,
            filter: 'eq(run_type, "chain")',
            selects: ["ID", "NAME", "RUN_TYPE", "TAGS", "START_TIME"],
          });
          const agents = (children.items ?? [])
            .map((item) => parseSummaryStep(item.name ?? ""))
            .filter((summary): summary is AgentSummary => summary !== undefined);
          if (agents.length === 0) continue;   // a run without the gate is not part of the fleet
          const versions = (children.items ?? []).map((item) => policyVersionFromTags(item.tags)).filter((version): version is number => version !== null);
          let necessity: number | null = null;
          let integrity: FleetRunRecord["integrity"] = "unknown";
          for await (const feedback of client.listFeedback({ runIds: [root.id], feedbackKeys: ["data_necessity_v1"] })) {
            necessity = typeof feedback.score === "number" ? feedback.score : null;
            integrity = integrityFromComment(feedback.comment);
          }
          records.push({
            runId: root.id, runName: root.name ?? "", startedAt: root.start_time ?? "",
            policyVersion: versions.length ? Math.max(...versions) : null,
            agents, necessity, integrity,
          });
        }
      }
      return records.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
    },
  };
}

/** One JSON record per line. Written by the run itself, read back by the report. */
export function appendFleetRunRecord(path: string, record: FleetRunRecord) {
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(record)}\n`);
}

export function ledgerDecisionSource(path: string): DecisionSource {
  return {
    name: `ledger:${path}`,
    async runs(period) {
      if (!existsSync(path)) return [];
      return readFileSync(path, "utf8").split("\n").filter(Boolean)
        .map((line) => JSON.parse(line) as FleetRunRecord)
        .filter((record) => { const at = new Date(record.startedAt); return at >= period.since && at <= period.until; })
        .sort((a, b) => b.startedAt.localeCompare(a.startedAt));
    },
  };
}

// ---- the report ----------------------------------------------------------

export type FleetReport = Readonly<{
  source: string;
  period: Readonly<{ since: string; until: string }>;
  policyVersions: readonly number[];
  totals: Readonly<{
    runs: number;
    clean: number;             // the gate had nothing to catch
    gateActed: number;         // at least one block, drop or strip
    blocked: number;
    dropped: number;
    stripped: number;
    necessityScored: number;
    necessityAverage: number | null;
    integrityFlags: number;
    highestTier?: string;
  }>;
  agents: readonly Readonly<{
    agent: string; runs: number; clean: number; blocked: number; dropped: number; stripped: number; highestTier?: string;
  }>[];
  gateActedRuns: readonly Readonly<{ startedAt: string; runId: string; runName: string; blocked: number; dropped: number; stripped: number; integrity: string }>[];
  incidents: Readonly<{ open: number; acknowledged: number; remediated: number; closed: number; rows: readonly IncidentRecord[] }>;
}>;

export function buildFleetReport(records: readonly FleetRunRecord[], source: string, period: Period, policy?: ScopePolicy, incidents: readonly IncidentRecord[] = []): FleetReport {
  const acted = (record: FleetRunRecord) => record.agents.some((agent) => agent.blocked + agent.dropped + agent.stripped > 0);
  const sum = (pick: (agent: AgentSummary) => number) => records.reduce((total, record) => total + record.agents.reduce((inner, agent) => inner + pick(agent), 0), 0);
  const top = (summaries: readonly { highestTier?: string }[]) =>
    policy ? highestTier(policy, summaries.map((summary) => ({ tier: summary.highestTier }))) : summaries.map((summary) => summary.highestTier).filter(Boolean).sort().at(-1);

  const byAgent = new Map<string, { runs: number; clean: number; blocked: number; dropped: number; stripped: number; tiers: { highestTier?: string }[] }>();
  for (const record of records) {
    for (const agent of record.agents) {
      const row = byAgent.get(agent.agent) ?? { runs: 0, clean: 0, blocked: 0, dropped: 0, stripped: 0, tiers: [] };
      row.runs++;
      if (agent.blocked + agent.dropped + agent.stripped === 0) row.clean++;
      row.blocked += agent.blocked; row.dropped += agent.dropped; row.stripped += agent.stripped;
      row.tiers.push({ highestTier: agent.highestTier });
      byAgent.set(agent.agent, row);
    }
  }
  const scored = records.filter((record) => record.necessity !== null);
  const overallTop = top(records.flatMap((record) => record.agents));
  return {
    source,
    period: { since: period.since.toISOString(), until: period.until.toISOString() },
    policyVersions: [...new Set(records.map((record) => record.policyVersion).filter((version): version is number => version !== null))].sort((a, b) => a - b),
    totals: {
      runs: records.length,
      clean: records.filter((record) => !acted(record)).length,
      gateActed: records.filter(acted).length,
      blocked: sum((agent) => agent.blocked), dropped: sum((agent) => agent.dropped), stripped: sum((agent) => agent.stripped),
      necessityScored: scored.length,
      necessityAverage: scored.length ? scored.reduce((total, record) => total + (record.necessity ?? 0), 0) / scored.length : null,
      integrityFlags: records.filter((record) => record.integrity === "cited_not_fetched").length,
      ...(overallTop ? { highestTier: overallTop } : {}),
    },
    agents: [...byAgent.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([agent, row]) => {
      const tier = top(row.tiers);
      return { agent, runs: row.runs, clean: row.clean, blocked: row.blocked, dropped: row.dropped, stripped: row.stripped, ...(tier ? { highestTier: tier } : {}) };
    }),
    incidents: {
      open: incidents.filter((incident) => incident.state === "open").length,
      acknowledged: incidents.filter((incident) => incident.state === "acknowledged").length,
      remediated: incidents.filter((incident) => incident.state === "remediated").length,
      closed: incidents.filter((incident) => incident.state === "closed").length,
      rows: incidents,
    },
    gateActedRuns: records.filter(acted).map((record) => ({
      startedAt: record.startedAt, runId: record.runId, runName: record.runName,
      blocked: record.agents.reduce((total, agent) => total + agent.blocked, 0),
      dropped: record.agents.reduce((total, agent) => total + agent.dropped, 0),
      stripped: record.agents.reduce((total, agent) => total + agent.stripped, 0),
      integrity: record.integrity,
    })),
  };
}

const pct = (part: number, whole: number) => whole === 0 ? "n/a" : `${Math.round((part / whole) * 100)}%`;

export function renderFleetReport(report: FleetReport): string {
  const t = report.totals;
  const lines: string[] = [];
  lines.push("# Data scope fleet report");
  lines.push("");
  lines.push(`Period ${report.period.since.slice(0, 10)} to ${report.period.until.slice(0, 10)}. Source: ${report.source}. Policy versions seen: ${report.policyVersions.join(", ") || "none"}.`);
  lines.push("");
  lines.push("Every run below ran behind the data scope gate, so every rule was enforced on every tool call. \"Clean\" means the gate had nothing to catch. \"Gate acted\" means it blocked a call, dropped out-of-scope records, or stripped forbidden fields, and the run was marked accordingly. Counts only; no data appears in this report.");
  lines.push("");
  lines.push("## Across the fleet");
  lines.push("");
  lines.push("| Runs | Clean | Gate acted | Blocked calls | Records dropped | Fields stripped | Necessity (avg) | Integrity flags | Highest tier |");
  lines.push("|---|---|---|---|---|---|---|---|---|");
  lines.push(`| ${t.runs} | ${t.clean} (${pct(t.clean, t.runs)}) | ${t.gateActed} (${pct(t.gateActed, t.runs)}) | ${t.blocked} | ${t.dropped} | ${t.stripped} | ${t.necessityAverage === null ? "n/a" : t.necessityAverage.toFixed(2)} over ${t.necessityScored} | ${t.integrityFlags} | ${t.highestTier ?? "n/a"} |`);
  lines.push("");
  lines.push("## Per agent");
  lines.push("");
  lines.push("| Agent | Runs | Clean | Blocked | Dropped | Stripped | Highest tier |");
  lines.push("|---|---|---|---|---|---|---|");
  for (const agent of report.agents) lines.push(`| \`${agent.agent}\` | ${agent.runs} | ${agent.clean} (${pct(agent.clean, agent.runs)}) | ${agent.blocked} | ${agent.dropped} | ${agent.stripped} | ${agent.highestTier ?? "n/a"} |`);
  lines.push("");
  lines.push("## Runs where the gate acted");
  lines.push("");
  if (report.gateActedRuns.length === 0) lines.push("None in this period.");
  else {
    lines.push("| When | Run | Blocked | Dropped | Stripped | Integrity |");
    lines.push("|---|---|---|---|---|---|");
    for (const run of report.gateActedRuns) lines.push(`| ${run.startedAt.slice(0, 16).replace("T", " ")} | ${run.runName} (\`${run.runId.slice(0, 8)}\`) | ${run.blocked} | ${run.dropped} | ${run.stripped} | ${run.integrity} |`);
  }
  lines.push("");
  lines.push("## Incidents and remediation");
  lines.push("");
  const i = report.incidents;
  lines.push(`Open ${i.open}, acknowledged ${i.acknowledged}, remediated ${i.remediated}, closed ${i.closed}. An incident is a gate action on a sensitive-tier source, a report citing evidence the gate never let through, or necessity below the floor for several runs in a row. Each has an owner and moves open, acknowledged, remediated, closed.`);
  lines.push("");
  if (i.rows.length === 0) lines.push("None on file.");
  else {
    lines.push("| State | Type | Agent | Tier | When | Owner | Detail |");
    lines.push("|---|---|---|---|---|---|---|");
    for (const row of i.rows) lines.push(`| ${row.state} | ${row.type} | \`${row.agent}\` | ${row.tier ?? "n/a"} | ${row.startedAt.slice(0, 16).replace("T", " ")} | ${row.owner} | ${row.detail} |`);
  }
  lines.push("");
  return lines.join("\n");
}
