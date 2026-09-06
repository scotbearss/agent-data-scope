import { readFileSync } from "node:fs";
import { ToolMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import { createMiddleware } from "langchain";
import { parse } from "yaml";
import { z } from "zod";

/**
 * Data scope gate.
 *
 * Step three: one hook (wrapToolCall) and one rule (permit by tool name).
 * Step four adds three more things, all inside the same hook:
 *   - scope:    a permitted read is checked against the data that comes back.
 *               Records from any other incident are dropped before the model
 *               sees them. If nothing is left, the read counts as blocked.
 *   - forbid:   fields on the forbid list are stripped from every result.
 *   - on_block: the agent's owner decides whether a blocked call stops the
 *               run (default) or lets the model continue with a refusal.
 * Every decision is one evidence line (names and counts, never data) and one
 * named step in the trace. At the end of the run a summary step is added.
 */

const OnBlock = z.enum(["stop", "continue"]);

const PermitRule = z.strictObject({
  tool: z.string().optional(),
  read: z.string().optional(),
  tier: z.string().optional(),
  fields: z.array(z.string()).optional(),
  scope: z.string().optional(),
});

const Classification = z.strictObject({
  scheme: z.string().min(1),
  tiers: z.array(z.strictObject({ label: z.string().min(1), meaning: z.string().min(1) })).min(1),
});

const Approval = z.strictObject({
  status: z.enum(["draft", "approved", "changes_requested"]),
  owner: z.string().min(1),
  approved_by: z.string().nullable(),
  approved_on: z.string().nullable(),
  change_log: z.array(z.strictObject({ version: z.number().int().positive(), on: z.string().min(1), change: z.string().min(1) })).min(1),
});

const AgentBlock = z.strictObject({
  permit: z.array(PermitRule).default([]),
  expect: z.array(z.record(z.string(), z.string())).optional(),
  on_block: OnBlock.optional(),
  owner: z.string().min(1).optional(),
});

const IncidentRules = z.strictObject({
  owner: z.string().min(1),
  sensitive_tiers: z.array(z.string().min(1)).min(1),
  necessity_floor: z.number().min(0).max(1),
  necessity_streak: z.number().int().positive(),
  feedback_key: z.string().min(1),
});

export const ScopePolicy = z.strictObject({
  version: z.number().int().positive(),
  agent_group: z.string().min(1),
  purpose: z.string().min(1),
  summary: z.array(z.string().min(1)).optional(),
  classification: Classification.optional(),
  approval: Approval.optional(),
  incidents: IncidentRules.optional(),
  defaults: z.strictObject({
    unlisted_tools: z.enum(["block", "permit"]),
    writes: z.literal("forbid"),
    on_block: OnBlock.default("stop"),
  }),
  forbid: z.strictObject({ fields_everywhere: z.array(z.string()) }),
  agents: z.record(z.string(), AgentBlock),
}).superRefine((policy, context) => {
  // A declared tier must be one the organization defined, and once a
  // classification exists every permitted source must carry a tier.
  const labels = new Set(policy.classification?.tiers.map((tier) => tier.label) ?? []);
  for (const tier of policy.incidents?.sensitive_tiers ?? []) {
    if (!labels.has(tier)) context.addIssue({ code: "custom", message: `incidents.sensitive_tiers: ${tier} is not in the classification` });
  }
  for (const [agent, block] of Object.entries(policy.agents)) {
    for (const rule of block.permit) {
      const source = rule.tool ?? rule.read ?? "?";
      if (rule.tier && !labels.has(rule.tier)) context.addIssue({ code: "custom", message: `${agent}.${source}: tier ${rule.tier} is not in the classification` });
      if (policy.classification && !rule.tier) context.addIssue({ code: "custom", message: `${agent}.${source}: missing tier` });
    }
  }
});
export type ScopePolicy = z.infer<typeof ScopePolicy>;

/** Rank of a tier label in the policy's classification, 0 = least sensitive. */
export function tierRank(policy: ScopePolicy, label: string | undefined) {
  if (!label || !policy.classification) return -1;
  return policy.classification.tiers.findIndex((tier) => tier.label === label);
}

/** The most sensitive tier among decisions, by the policy's own ordering. */
export function highestTier(policy: ScopePolicy, decisions: readonly { tier?: string }[]) {
  let best: string | undefined;
  for (const decision of decisions) if (tierRank(policy, decision.tier) > tierRank(policy, best)) best = decision.tier;
  return best;
}

/** Read and validate a policy file. A malformed policy fails here, never at run time. */
export function loadScopePolicy(path: string): ScopePolicy {
  return ScopePolicy.parse(parse(readFileSync(path, "utf8")));
}

/** What the gate knows about the job it is guarding. Values only, never data. */
export type ScopeContext = Readonly<Record<string, Readonly<Record<string, string | undefined>> | undefined>>;

/** One line of evidence. Names and counts only, never the data itself. */
export type GateDecision = Readonly<{
  at: string;
  policyVersion: number;
  agent: string;
  tool: string;
  decision: "permit" | "block";
  rule: string;
  /** Sensitivity tier declared for this source in the policy. Absent for unlisted tools. */
  tier?: string;
  recordsChecked?: number;
  recordsDropped?: number;
  strippedFields?: readonly string[];
  /** Identifiers of the records the model was allowed to see. Names, never contents. */
  recordRefs?: readonly string[];
}>;

export class DataScopeStopError extends Error {
  constructor(message: string, readonly gateDecision: GateDecision) {
    super(message);
    this.name = "DataScopeStopError";
  }
}

/** Collects decisions across one run so the outcome can be marked honestly. */
export function createScopeLedger(policy?: ScopePolicy) {
  const decisions: GateDecision[] = [];
  const summary = () => ({
    highestTier: policy ? highestTier(policy, decisions) : undefined,
    permitted: decisions.filter((decision) => decision.decision === "permit").length,
    blocked: decisions.filter((decision) => decision.decision === "block").length,
    recordsDropped: decisions.reduce((total, decision) => total + (decision.recordsDropped ?? 0), 0),
    fieldsStripped: decisions.reduce((total, decision) => total + (decision.strippedFields?.length ?? 0), 0),
  });
  const mark = () => {
    const totals = summary();
    const notes = [
      totals.blocked ? `${totals.blocked} blocked read${totals.blocked === 1 ? "" : "s"}` : "",
      totals.recordsDropped ? `${totals.recordsDropped} record${totals.recordsDropped === 1 ? "" : "s"} dropped` : "",
      totals.fieldsStripped ? `${totals.fieldsStripped} field${totals.fieldsStripped === 1 ? "" : "s"} stripped` : "",
    ].filter(Boolean);
    return notes.length ? `completed with ${notes.join(", ")}` : "completed within scope";
  };
  return { decisions, record: (decision: GateDecision) => { decisions.push(decision); }, summary, mark };
}

// ---- helpers -------------------------------------------------------------

function normalizeKey(key: string) {
  return key.toLowerCase().replace(/[_-]/g, "");
}

function readField(record: unknown, field: string): unknown {
  if (!record || typeof record !== "object") return undefined;
  const wanted = normalizeKey(field);
  for (const [key, value] of Object.entries(record)) {
    if (normalizeKey(key) === wanted) return value;
  }
  return undefined;
}

function parseScope(scope: string) {
  const match = /^\s*([a-z_]+)\s*==\s*([a-z_]+(?:\.[a-z_]+)+)\s*$/.exec(scope);
  if (!match) throw new Error(`Unsupported scope expression: ${scope}`);
  return { field: match[1], path: match[2].split(".") };
}

function resolveContext(context: ScopeContext, path: readonly string[]) {
  let current: unknown = context;
  for (const segment of path) current = current && typeof current === "object" ? (current as Record<string, unknown>)[segment] : undefined;
  return typeof current === "string" && current.length > 0 ? current : undefined;
}

/** Remove forbidden keys anywhere in a JSON value. Returns the names removed. */
function stripForbidden(value: unknown, forbidden: ReadonlySet<string>, removed: string[]): unknown {
  if (Array.isArray(value)) return value.map((item) => stripForbidden(item, forbidden, removed));
  if (!value || typeof value !== "object") return value;
  const output: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value)) {
    if (forbidden.has(normalizeKey(key))) { removed.push(key); continue; }
    output[key] = stripForbidden(child, forbidden, removed);
  }
  return output;
}

async function traceStep(runName: string, policyVersion: number, tier?: string) {
  await RunnableLambda.from(async () => runName)
    .withConfig({ runName, tags: ["data-scope-gate", `policy-v${policyVersion}`, ...(tier ? [`tier-${tier}`] : [])] })
    .invoke({});
}

// ---- the gate ------------------------------------------------------------

export function createDataScopeGate(options: {
  policy: ScopePolicy;
  agent: string;
  context?: ScopeContext;
  onDecision?: (decision: GateDecision) => void;
}) {
  const { policy, agent } = options;
  const block = policy.agents[agent];
  if (!block) throw new Error(`Scope policy v${policy.version} has no block for agent ${agent}`);
  const onBlock = block.on_block ?? policy.defaults.on_block;
  const forbidden = new Set(policy.forbid.fields_everywhere.map(normalizeKey));
  const context = options.context ?? {};

  // Resolve every scope up front, so a missing context value fails when the
  // agent is built, not in the middle of a run.
  const rules = new Map<string, { tier?: string; scope?: { field: string; expected: string; path: string } }>();
  for (const rule of block.permit) {
    if (!rule.tool) continue;
    if (!rule.scope) { rules.set(rule.tool, { tier: rule.tier }); continue; }
    const parsed = parseScope(rule.scope);
    const expected = resolveContext(context, parsed.path);
    if (!expected) throw new Error(`Scope for ${agent}.${rule.tool} needs context ${parsed.path.join(".")}`);
    rules.set(rule.tool, { tier: rule.tier, scope: { field: parsed.field, expected, path: parsed.path.join(".") } });
  }

  const local: GateDecision[] = [];
  const stamp = (decision: GateDecision) => { local.push(decision); options.onDecision?.(decision); };
  const base = (tool: string) => ({ at: new Date().toISOString(), policyVersion: policy.version, agent, tool });

  const refuse = async (decision: GateDecision, request: { toolCall: { id?: string; name: string } }) => {
    stamp(decision);
    await traceStep(`DataScopeGate block ${decision.tool} | ${decision.rule}`, policy.version, decision.tier);
    const message = `Blocked by data scope policy v${policy.version}: ${decision.tool} for ${agent} (${decision.rule}).`;
    if (onBlock === "stop") throw new DataScopeStopError(`Data scope gate stopped the run. ${message}`, decision);
    return new ToolMessage({ tool_call_id: request.toolCall.id ?? "", name: decision.tool, status: "error", content: message });
  };

  return createMiddleware({
    name: `DataScopeGate-${agent}`,
    wrapToolCall: async (request, handler) => {
      const tool = request.toolCall.name;
      const rule = rules.get(tool);

      // 1. Permit by name. Unlisted tools never run.
      if (!rule) {
        if (policy.defaults.unlisted_tools === "block") {
          return refuse({ ...base(tool), decision: "block", rule: "defaults.unlisted_tools" }, request);
        }
        stamp({ ...base(tool), decision: "permit", rule: "defaults.unlisted_tools" });
        await traceStep(`DataScopeGate permit ${tool} | defaults.unlisted_tools`, policy.version);
        return handler(request);
      }

      const tiered = (tool: string) => ({ ...base(tool), ...(rule.tier ? { tier: rule.tier } : {}) });
      const result = await handler(request);
      if (!(result instanceof ToolMessage) || typeof result.content !== "string") {
        stamp({ ...tiered(tool), decision: "permit", rule: `agents.${agent}.permit` });
        await traceStep(`DataScopeGate permit ${tool}`, policy.version, rule.tier);
        return result;
      }

      let parsed: unknown;
      try { parsed = JSON.parse(result.content); } catch { parsed = undefined; }

      // 2. Scope. A permitted read is checked against what came back.
      let recordsChecked: number | undefined;
      let recordsDropped: number | undefined;
      let recordRefs: string[] | undefined;
      if (rule.scope) {
        const container = parsed && typeof parsed === "object" && Array.isArray((parsed as { records?: unknown }).records)
          ? (parsed as { records: unknown[] }) : undefined;
        const records = container ? container.records : parsed === undefined ? undefined : [parsed];
        if (!records) {
          return refuse({ ...tiered(tool), decision: "block", rule: `agents.${agent}.permit.scope unverifiable` }, request);
        }
        const kept = records.filter((record) => readField(record, rule.scope!.field) === rule.scope!.expected);
        recordsChecked = records.length;
        recordsDropped = records.length - kept.length;
        if (kept.length === 0) {
          return refuse({ ...tiered(tool), decision: "block", rule: `agents.${agent}.permit.scope`, recordsChecked, recordsDropped }, request);
        }
        parsed = container ? { ...container, records: kept } : kept[0];
        const refs = kept.map((record) => readField(record, "ref"));
        if (refs.every((ref): ref is string => typeof ref === "string" && ref.length > 0)) recordRefs = refs;
      }

      // 3. Forbid. Listed fields are stripped before the model sees the result.
      const strippedFields: string[] = [];
      if (parsed !== undefined) parsed = stripForbidden(parsed, forbidden, strippedFields);

      const decision: GateDecision = {
        ...tiered(tool), decision: "permit", rule: `agents.${agent}.permit`,
        ...(recordsChecked !== undefined ? { recordsChecked, recordsDropped } : {}),
        ...(recordRefs ? { recordRefs } : {}),
        ...(strippedFields.length ? { strippedFields } : {}),
      };
      stamp(decision);
      const notes = [
        recordsDropped ? `dropped ${recordsDropped}` : "",
        strippedFields.length ? `stripped ${strippedFields.join(",")}` : "",
      ].filter(Boolean);
      await traceStep(`DataScopeGate permit ${tool}${notes.length ? ` | ${notes.join(" | ")}` : ""}`, policy.version, rule.tier);

      if (parsed === undefined || (recordsChecked === undefined && strippedFields.length === 0)) return result;
      return new ToolMessage({ tool_call_id: result.tool_call_id, name: result.name ?? tool, status: result.status, content: JSON.stringify(parsed) });
    },
    afterAgent: async () => {
      const permitted = local.filter((decision) => decision.decision === "permit").length;
      const blocked = local.filter((decision) => decision.decision === "block").length;
      const dropped = local.reduce((total, decision) => total + (decision.recordsDropped ?? 0), 0);
      const stripped = local.reduce((total, decision) => total + (decision.strippedFields?.length ?? 0), 0);
      const top = highestTier(policy, local);
      await traceStep(`DataScopeGate summary ${agent} | permitted ${permitted} | blocked ${blocked} | dropped ${dropped} | stripped ${stripped}${top ? ` | highest tier ${top}` : ""}`, policy.version, top);
      return undefined;
    },
  });
}
