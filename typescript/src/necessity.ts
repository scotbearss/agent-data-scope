import type { Client } from "langsmith";
import type { GateDecision } from "./gate.js";

/**
 * Necessity scoring, step five. Deterministic: no model is asked anything.
 *
 * The gate records which record identifiers each agent was allowed to see
 * (fetched). Each agent's report cites the identifiers it relied on (cited).
 * Fetched and never cited is the first, cheapest evidence of "allowed but
 * not needed". Cited but never fetched is an integrity problem: the report
 * points at evidence the agent did not read through the gate.
 *
 * Limits, stated plainly: a record can be needed without being cited (used to
 * rule something out), and citing everything would game the ratio. This is a
 * floor, not a verdict. A relevance map or a model judge can sit on top.
 */

export type AgentNecessity = Readonly<{
  agent: string;
  measurable: boolean;          // false when the agent's reads carry no identifiers
  fetched: readonly string[];
  cited: readonly string[];
  used: readonly string[];      // fetched and cited
  unused: readonly string[];    // fetched, never cited
  citedNotFetched: readonly string[];
  necessity: number | null;     // used / fetched, or null when not measurable
}>;

export type NecessityScore = Readonly<{
  version: 1;
  method: "fetched-vs-cited";
  agents: readonly AgentNecessity[];
  overall: Readonly<{ fetched: number; used: number; unused: number; necessity: number | null; integrity: "ok" | "cited_not_fetched" }>;
}>;

export function scoreNecessity(
  decisions: readonly GateDecision[],
  citations: ReadonlyMap<string, readonly string[]>,
): NecessityScore {
  const agents = [...new Set([...decisions.map((decision) => decision.agent), ...citations.keys()])].sort();
  const perAgent = agents.map((agent): AgentNecessity => {
    const reads = decisions.filter((decision) => decision.agent === agent && decision.decision === "permit");
    const measurable = reads.length > 0 && reads.some((decision) => decision.recordRefs !== undefined);
    const fetched = [...new Set(reads.flatMap((decision) => decision.recordRefs ?? []))].sort();
    const cited = [...new Set(citations.get(agent) ?? [])].sort();
    const fetchedSet = new Set(fetched);
    const citedSet = new Set(cited);
    const used = fetched.filter((ref) => citedSet.has(ref));
    const unused = fetched.filter((ref) => !citedSet.has(ref));
    const citedNotFetched = measurable ? cited.filter((ref) => !fetchedSet.has(ref)) : [];
    return {
      agent, measurable, fetched, cited, used, unused, citedNotFetched,
      necessity: measurable && fetched.length > 0 ? used.length / fetched.length : null,
    };
  });
  const measured = perAgent.filter((agent) => agent.measurable);
  const fetched = measured.reduce((total, agent) => total + agent.fetched.length, 0);
  const used = measured.reduce((total, agent) => total + agent.used.length, 0);
  return {
    version: 1,
    method: "fetched-vs-cited",
    agents: perAgent,
    overall: {
      fetched, used, unused: fetched - used,
      necessity: fetched > 0 ? used / fetched : null,
      integrity: perAgent.some((agent) => agent.citedNotFetched.length > 0) ? "cited_not_fetched" : "ok",
    },
  };
}

/** The LangSmith feedback row. Names and counts only. */
export function necessityFeedback(score: NecessityScore) {
  const unused = score.agents.flatMap((agent) => agent.unused.map((ref) => `${agent.agent}: ${ref}`));
  return {
    key: "data_necessity_v1",
    score: score.overall.necessity,
    comment: [
      `Deterministic fetched-vs-cited check; not a human quality rating.`,
      `fetched ${score.overall.fetched}, used ${score.overall.used}, unused ${score.overall.unused}, integrity ${score.overall.integrity}.`,
      unused.length ? `Unused: ${unused.join("; ")}` : "Nothing fetched went uncited.",
    ].join(" "),
  };
}

/**
 * Feedback posted before LangSmith has finished ingesting the run is accepted
 * and silently dropped (observed 2026-09-06). Wait until the run is readable.
 */
export async function waitForRun(client: Client, runId: string, projectId: string, deadlineMs: number) {
  const started = Date.now();
  for (;;) {
    try {
      await client.runs.retrieve(runId, { project_id: projectId, selects: ["ID", "STATUS"] });
      return Date.now() - started;
    } catch (error) {
      if (Date.now() - started > deadlineMs) throw new Error(`Run ${runId} was not readable within ${deadlineMs}ms`, { cause: error });
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    }
  }
}

export async function recordNecessityFeedback(client: Client, runId: string, projectName: string, score: NecessityScore, options: { deadlineMs?: number } = {}) {
  const project = await client.readProject({ projectName });
  const waitedMs = await waitForRun(client, runId, project.id, options.deadlineMs ?? 60_000);
  const feedback = necessityFeedback(score);
  await client.createFeedback({ runId, sessionId: project.id, key: feedback.key, score: feedback.score ?? undefined, comment: feedback.comment, feedbackSourceType: "api", extendTraceRetention: false });
  return { ...feedback, waitedMs };
}
