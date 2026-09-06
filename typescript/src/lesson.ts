import { AIMessage } from "@langchain/core/messages";
import type { RunnableConfig } from "@langchain/core/runnables";
import { tool } from "@langchain/core/tools";
import { FakeListChatModel } from "@langchain/core/utils/testing";
import { createDeepAgent, StateBackend } from "deepagents";
import { z } from "zod";
import { createDataScopeGate, createScopeLedger, loadScopePolicy, type ScopePolicy } from "./gate.js";
import { scoreNecessity } from "./necessity.js";

/**
 * A tiny fictional agent for the data scope lesson. No model is paid for:
 * the "model" is a script that asks for two tools, one permitted and one not.
 * The permitted tool returns two records, two from the right case and one
 * from another, and one of them carries a forbidden field.
 */

export const LESSON_AGENT = "example-investigator";
export const LESSON_POLICY_PATH = "../policies/example-scope.yaml";
export const LESSON_INCIDENT = "inc-fictional-0001";
const EMPTY = z.strictObject({});

export const LESSON_SCI_TOOL = "get_support_message_preview";

export function createDataScopeLessonAgent(
  model: FakeListChatModel,
  options: { onBlock?: ScopePolicy["defaults"]["on_block"]; policyPath?: string; sciProbe?: boolean } = {},
) {
  const loaded = loadScopePolicy(options.policyPath ?? LESSON_POLICY_PATH);
  // Lesson-only overrides, applied to a copy of the policy: the owner's on_block
  // choice, and (for the incident lesson) one extra permitted source at the
  // most sensitive tier, so a strip on it can be watched becoming an incident.
  const block = loaded.agents[LESSON_AGENT];
  const policy: ScopePolicy = {
    ...loaded,
    agents: { ...loaded.agents, [LESSON_AGENT]: {
      ...block,
      ...(options.onBlock ? { on_block: options.onBlock } : {}),
      permit: options.sciProbe ? [...block.permit, { tool: LESSON_SCI_TOOL, tier: "SCI" }] : block.permit,
    } },
  };
  const ledger = createScopeLedger(policy);
  const ran = { permittedTool: 0, forbiddenTool: 0, sciTool: 0 };

  const platformAggregate = tool(async () => {
    ran.permittedTool++;
    return JSON.stringify({ version: 1, records: [
      { ref: "snapshot:fictional:signals", incidentId: LESSON_INCIDENT, kind: "signals", value: { fictional: true, signals: ["delivery_failure"] }, email_address: "fictional@example.invalid" },
      { ref: "snapshot:fictional:heartbeat", incidentId: LESSON_INCIDENT, kind: "heartbeat", value: { fictional: true, heartbeat: "fresh" } },
      { ref: "snapshot:fictional-other:platform", incidentId: "inc-fictional-9999", kind: "platform", value: { fictional: true, heartbeat: "stale" } },
    ] });
  }, { name: "get_incident_snapshot", description: "Read the fictional incident snapshot.", schema: EMPTY });

  const messagePreview = tool(async () => {
    ran.sciTool++;
    return JSON.stringify({ version: 1, records: [
      { ref: "message:fictional:1", incidentId: LESSON_INCIDENT, kind: "support-message", subject: "fictional subject", message_text: "fictional body that must never reach the model" },
    ] });
  }, { name: LESSON_SCI_TOOL, description: "Lesson only: a sensitive-tier source that returns a forbidden field.", schema: EMPTY });

  const householdRecords = tool(async () => {
    ran.forbiddenTool++;
    return JSON.stringify({ fictional: true, households: [] });
  }, { name: "get_household_records", description: "A tool this agent is not permitted to use.", schema: EMPTY });

  const agent = createDeepAgent({
    model,
    name: LESSON_AGENT,
    backend: new StateBackend(),
    subagents: [],
    tools: options.sciProbe ? [platformAggregate, householdRecords, messagePreview] : [platformAggregate, householdRecords],
    systemPrompt: "Fictional lesson agent. Read the incident snapshot, then report.",
    middleware: [createDataScopeGate({
      policy, agent: LESSON_AGENT,
      context: { incident: { id: LESSON_INCIDENT } },
      onDecision: ledger.record,
    })],
  });
  return { agent, policy, ledger, ran };
}

/** The script the fake model follows: call the tools, then answer. */
export function scriptedLessonModel(options: { sciProbe?: boolean } = {}) {
  const model = new FakeListChatModel({ responses: ["unused"] });
  model.getName = () => "ChatOpenAI";
  model.bindTools = () => model;
  let calls = 0;
  model._generate = async () => {
    const message = ++calls === 1
      ? new AIMessage({ content: "", tool_calls: [
          { name: "get_incident_snapshot", args: {}, id: "lesson-read-1", type: "tool_call" },
          { name: "get_household_records", args: {}, id: "lesson-read-2", type: "tool_call" },
          ...(options.sciProbe ? [{ name: LESSON_SCI_TOOL, args: {}, id: "lesson-read-3", type: "tool_call" as const }] : []),
        ] })
      : new AIMessage(JSON.stringify({ finding: "Fictional report: one delivery failure signal; household records were not available.", evidenceRefs: ["snapshot:fictional:signals"] }));
    return { generations: [{ text: String(message.content), message }] };
  };
  return model;
}

export async function runDataScopeLesson(
  session: ReturnType<typeof createDataScopeLessonAgent>,
  traceConfig: Pick<RunnableConfig, "callbacks" | "runId" | "runName" | "tags"> = {},
) {
  const result = await session.agent.invoke(
    { messages: [{ role: "user", content: "Run the fictional data scope lesson." }] },
    { ...traceConfig, recursionLimit: 8, signal: AbortSignal.timeout(30_000) },
  );
  const toolMessages = result.messages.filter((message) => message.type === "tool");
  const finalText = String(result.messages.at(-1)?.content ?? "");
  let citedRefs: string[] = [];
  try {
    const parsed = JSON.parse(finalText) as { evidenceRefs?: unknown };
    if (Array.isArray(parsed.evidenceRefs)) citedRefs = parsed.evidenceRefs.filter((ref): ref is string => typeof ref === "string");
  } catch { /* a run that ends without a report cites nothing */ }
  const necessity = scoreNecessity(session.ledger.decisions, new Map([[LESSON_AGENT, citedRefs]]));
  return {
    finalText,
    citedRefs,
    necessity,
    toolMessages,
    decisions: session.ledger.decisions,
    summary: session.ledger.summary(),
    mark: session.ledger.mark(),
    ran: session.ran,
  };
}
