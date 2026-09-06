import { writeFileSync } from "node:fs";
import { loadScopePolicy } from "./gate.js";
import { policyCardIsCurrent, renderPolicyCard } from "./policy-card.js";

const POLICY = "../policies/example-scope.yaml";
const CARD = "../policies/example-scope.md";
const policy = loadScopePolicy(POLICY);
if (process.argv.includes("--check")) {
  if (!policyCardIsCurrent(policy, CARD)) { console.error(`${CARD} is out of date. Run npm run card.`); process.exit(1); }
  console.log("Policy card matches the policy file.");
} else {
  writeFileSync(CARD, renderPolicyCard(policy));
  console.log(`Wrote ${CARD} for policy version ${policy.version}.`);
}
