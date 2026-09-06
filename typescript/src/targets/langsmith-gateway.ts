import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { z } from "zod";
import type { ScopePolicy } from "../gate.js";

/**
 * LangSmith gateway adapter, step two: the planner.
 *
 * Reads our vendor-neutral policy and computes which LangSmith gateway
 * policies should exist for it. One kind is derived, and no policy field
 * exists for it: a `guard` (personal data and secrets detection at the model
 * boundary) for any agent that may read a source at a sensitive tier. The
 * policy stays about data scope; the adapter is a derivation, not a home for
 * platform settings. The gateway scopes policies by API key. Keys are a LangSmith fact, not a
 * governance fact, so they live in a separate bindings file, never in the policy.
 *
 * The planner is pure: policy + bindings + what exists -> a plan. It never
 * deletes. Policies it did not create are left alone and reported as orphans
 * only when they carry our marker.
 */

export const MARKER = "agent-data-scope";

export const GatewayBindings = z.strictObject({
  version: z.literal(1),
  agents: z.record(z.string(), z.strictObject({ api_key_id: z.string().uuid() })),
});
export type GatewayBindings = z.infer<typeof GatewayBindings>;

export function loadGatewayBindings(path: string): GatewayBindings {
  return GatewayBindings.parse(parse(readFileSync(path, "utf8")));
}

/** The subset of a gateway policy the planner reads and writes. */
export type GatewayPolicy = Readonly<{
  id?: string;
  name: string;
  description?: string;
  policy_type: "guard";
  action: "block";
  enabled: boolean;
  config: Readonly<Record<string, unknown>>;
  subject_matchers: readonly Readonly<{ key: string; value: string }>[];
}>;

export type GatewayPlan = Readonly<{
  policyVersion: number;
  create: readonly GatewayPolicy[];
  update: readonly Readonly<{ id: string; before: GatewayPolicy; after: GatewayPolicy; changed: readonly string[] }>[];
  unchanged: readonly GatewayPolicy[];
  skipped: readonly Readonly<{ agent: string; kind: "guard"; reason: string }>[];
  orphans: readonly GatewayPolicy[];
}>;

export function managedName(agent: string, kind: "guard" = "guard") {
  return `${MARKER}: ${agent} ${kind}`;
}

function desiredPolicies(policy: ScopePolicy, bindings: GatewayBindings) {
  const sensitive = new Set(policy.incidents?.sensitive_tiers ?? []);
  const desired: GatewayPolicy[] = [];
  const skipped: { agent: string; kind: "guard"; reason: string }[] = [];
  const description = `Managed by ${MARKER} from policy ${policy.agent_group} v${policy.version}. Edit the policy, not this.`;
  for (const [agent, block] of Object.entries(policy.agents)) {
    const key = bindings.agents[agent]?.api_key_id;
    const wantsGuard = block.permit.some((rule) => rule.tier && sensitive.has(rule.tier));
    if (wantsGuard) {
      if (!key) skipped.push({ agent, kind: "guard", reason: "no api_key_id binding" });
      else desired.push({
        name: managedName(agent, "guard"), description, policy_type: "guard", action: "block", enabled: true,
        config: { detect: { pii: true, secrets: true } },
        subject_matchers: [{ key: "api_key_id", value: key }],
      });
    }
  }
  return { desired, skipped };
}

const canonical = (value: unknown): string => JSON.stringify(value, (_key, v) =>
  v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))) : v);

function differences(before: GatewayPolicy, after: GatewayPolicy) {
  const changed: string[] = [];
  for (const field of ["policy_type", "action", "enabled", "config", "subject_matchers", "description"] as const) {
    if (canonical(before[field]) !== canonical(after[field])) changed.push(field);
  }
  return changed;
}

export function planGatewaySync(policy: ScopePolicy, bindings: GatewayBindings, existing: readonly GatewayPolicy[]): GatewayPlan {
  const { desired, skipped } = desiredPolicies(policy, bindings);
  const managed = existing.filter((candidate) => candidate.name.startsWith(`${MARKER}: `));
  const byName = new Map(managed.map((candidate) => [candidate.name, candidate]));
  const create: GatewayPolicy[] = [];
  const update: { id: string; before: GatewayPolicy; after: GatewayPolicy; changed: string[] }[] = [];
  const unchanged: GatewayPolicy[] = [];
  for (const want of desired) {
    const have = byName.get(want.name);
    if (!have) { create.push(want); continue; }
    const changed = differences(have, want);
    if (changed.length === 0) unchanged.push(have);
    else update.push({ id: have.id ?? "", before: have, after: { ...want, id: have.id }, changed });
  }
  const wanted = new Set(desired.map((want) => want.name));
  const orphans = managed.filter((candidate) => !wanted.has(candidate.name));
  return { policyVersion: policy.version, create, update, unchanged, skipped, orphans };
}

export function renderGatewayPlan(plan: GatewayPlan): string {
  const lines: string[] = [];
  const subject = (p: GatewayPolicy) => p.subject_matchers.map((m) => `${m.key}=${m.value.slice(0, 8)}…`).join(",");
  lines.push(`Gateway sync plan for policy v${plan.policyVersion} (dry run unless applied; never deletes)`);
  lines.push(`  create ${plan.create.length}, update ${plan.update.length}, unchanged ${plan.unchanged.length}, skipped ${plan.skipped.length}, orphans ${plan.orphans.length}`);
  for (const p of plan.create) lines.push(`  + create   ${p.name}  ${JSON.stringify(p.config)}  ${subject(p)}`);
  for (const u of plan.update) lines.push(`  ~ update   ${u.after.name}  changed: ${u.changed.join(", ")}  now ${JSON.stringify(u.after.config)}`);
  for (const p of plan.unchanged) lines.push(`  = keep     ${p.name}`);
  for (const s of plan.skipped) lines.push(`  ! skipped  ${s.agent} ${s.kind}: ${s.reason}`);
  for (const p of plan.orphans) lines.push(`  ? orphan   ${p.name} (managed by us, no longer in the policy; not deleted)`);
  return lines.join("\n");
}
