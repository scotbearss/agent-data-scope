import assert from "node:assert/strict";
import { loadScopePolicy } from "../src/gate.js";
import { GatewayBindings, managedName, planGatewaySync, renderGatewayPlan, type GatewayPolicy } from "../src/targets/langsmith-gateway.js";

globalThis.fetch = async () => { throw new Error("Offline only"); };
const policy = loadScopePolicy("../policies/example-scope.yaml");
assert.deepEqual(policy.agents["example-investigator"].limits, { spend: { window: "weekly", limit_usd: 5 } });

const KEY_A = "11111111-1111-4111-8111-111111111111";
const KEY_B = "22222222-2222-4222-8222-222222222222";
const bindings = GatewayBindings.parse({ version: 1, agents: { "example-investigator": { api_key_id: KEY_A }, "example-support-drafter": { api_key_id: KEY_B } } });
assert.throws(() => GatewayBindings.parse({ version: 1, agents: { x: { api_key_id: "not-a-uuid" } } }));

// Empty workspace: the investigator gets a spend cap (it has limits), the drafter gets a guard (it reads an SCI source).
{
  const plan = planGatewaySync(policy, bindings, []);
  assert.deepEqual(plan.create.map((p) => [p.name, p.policy_type]), [
    [managedName("example-investigator", "spend_cap"), "spend_cap"],
    [managedName("example-support-drafter", "guard"), "guard"],
  ]);
  const cap = plan.create[0];
  assert.deepEqual(cap.config, { window: "weekly", limit_usd: 5 });
  assert.deepEqual(cap.subject_matchers, [{ key: "api_key_id", value: KEY_A }]);
  assert.equal(cap.action, "block");
  assert.match(cap.description ?? "", /Managed by agent-data-scope from policy example v2/);
  assert.deepEqual(plan.create[1].config, { detect: { pii: true, secrets: true } });
  assert.deepEqual([plan.update, plan.unchanged, plan.skipped, plan.orphans], [[], [], [], []]);
}
// Existing and identical: nothing to do. Existing but different: an update with the changed fields named.
{
  const first = planGatewaySync(policy, bindings, []);
  const existing: GatewayPolicy[] = first.create.map((p, index) => ({ ...p, id: `id-${index}` }));
  const same = planGatewaySync(policy, bindings, existing);
  assert.equal(same.unchanged.length, 2);
  assert.deepEqual([same.create, same.update], [[], []]);

  const drifted: GatewayPolicy[] = [{ ...existing[0], config: { window: "weekly", limit_usd: 50 }, enabled: false }, existing[1]];
  const fix = planGatewaySync(policy, bindings, drifted);
  assert.equal(fix.update.length, 1);
  assert.equal(fix.update[0].id, "id-0");
  assert.deepEqual(fix.update[0].changed, ["enabled", "config"]);
  assert.deepEqual(fix.update[0].after.config, { window: "weekly", limit_usd: 5 });
}
// A binding we don't have: skipped and named, not guessed. A hand-made policy: untouched, not even reported.
{
  const partial = GatewayBindings.parse({ version: 1, agents: { "example-investigator": { api_key_id: KEY_A } } });
  const handMade: GatewayPolicy = { id: "hand", name: "Sentinel weekly model budget", policy_type: "spend_cap", action: "block", enabled: true, config: { window: "weekly", limit_usd: 5 }, subject_matchers: [{ key: "api_key_id", value: KEY_B }] };
  const plan = planGatewaySync(policy, partial, [handMade]);
  assert.deepEqual(plan.skipped, [{ agent: "example-support-drafter", kind: "guard", reason: "no api_key_id binding" }]);
  assert.deepEqual(plan.orphans, []);
  assert.equal(plan.create.length, 1);
}
// A policy we created for an agent that no longer exists: reported as an orphan, never deleted.
{
  const orphan: GatewayPolicy = { id: "old", name: managedName("retired-agent", "guard"), policy_type: "guard", action: "block", enabled: true, config: { detect: { pii: true, secrets: true } }, subject_matchers: [{ key: "api_key_id", value: KEY_B }] };
  const plan = planGatewaySync(policy, bindings, [orphan]);
  assert.deepEqual(plan.orphans.map((p) => p.name), [orphan.name]);
  const text = renderGatewayPlan(plan);
  assert.match(text, /create 2, update 0, unchanged 0, skipped 0, orphans 1/);
  assert.match(text, /\? orphan   agent-data-scope: retired-agent guard/);
  assert.ok(!text.includes(KEY_A), "the plan shows key prefixes only");
}
console.log("LangSmith gateway planner passed: spend caps and guards derived from the policy, keys from bindings, diffs named, orphans reported, nothing deleted.");
