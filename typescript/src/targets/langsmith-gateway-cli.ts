import { loadScopePolicy } from "../gate.js";
import { loadGatewayBindings, planGatewaySync, renderGatewayPlan, type GatewayPolicy } from "./langsmith-gateway.js";

/**
 * npm run derive -- [--policy ../policies/example-scope.yaml] [--bindings local/langsmith-bindings.yaml] [--json]
 * Read-only. Lists the workspace's gateway policies, derives what the policy implies,
 * and prints the plan. It never creates, updates, or deletes anything.
 */
const args = new Map<string, string>();
for (let index = 2; index < process.argv.length; index += 1) {
  const arg = process.argv[index];
  if (!arg.startsWith("--")) continue;
  const next = process.argv[index + 1];
  if (next && !next.startsWith("--")) { args.set(arg.slice(2), next); index += 1; } else args.set(arg.slice(2), "true");
}
const key = process.env.LANGSMITH_API_KEY;
if (!key) { console.error("LANGSMITH_API_KEY is not set. This command only reads, but it needs a key to list gateway policies."); process.exit(1); }

const policy = loadScopePolicy(args.get("policy") ?? "../policies/example-scope.yaml");
const bindings = loadGatewayBindings(args.get("bindings") ?? "local/langsmith-bindings.yaml");
const endpoint = process.env.LANGSMITH_ENDPOINT ?? "https://api.smith.langchain.com";
const response = await fetch(`${endpoint}/api/v1/platform/gateway-policies`, { headers: { "x-api-key": key }, signal: AbortSignal.timeout(15_000), redirect: "error" });
if (!response.ok) { console.error(`Could not list gateway policies: HTTP ${response.status}`); process.exit(1); }
const body = await response.json() as unknown;
const rows = (Array.isArray(body) ? body : (body as { policies?: unknown[] }).policies ?? []) as Record<string, unknown>[];
const existing: GatewayPolicy[] = rows.map((row) => ({
  id: String(row.id ?? ""), name: String(row.name ?? ""), description: typeof row.description === "string" ? row.description : undefined,
  policy_type: row.policy_type as GatewayPolicy["policy_type"], action: "block", enabled: Boolean(row.enabled),
  config: (row.config ?? {}) as Record<string, unknown>,
  subject_matchers: ((row.subject_matchers ?? []) as { key: string; value: string }[]).map((m) => ({ key: m.key, value: String(m.value) })),
}));

const plan = planGatewaySync(policy, bindings, existing);
if (args.get("json")) console.log(JSON.stringify({ existing: existing.length, plan }, null, 2));
else {
  console.log(`Workspace has ${existing.length} gateway polic${existing.length === 1 ? "y" : "ies"}: ${existing.map((p) => `${p.name} [${p.policy_type}]`).join("; ") || "none"}`);
  console.log(renderGatewayPlan(plan));
}
