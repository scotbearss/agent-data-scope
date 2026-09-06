import { readFileSync } from "node:fs";
import type { ScopePolicy } from "./gate.js";

/**
 * The policy card, step six. Governance reads this, not the YAML and not the
 * traces. It is generated from the policy file so the two cannot drift: the
 * plain-English summary on top, the enforced rules underneath, sensitivity
 * tiers per source, and the approval record.
 */

function code(text: string) { return `\`${text}\``; }

export function renderPolicyCard(policy: ScopePolicy, sourcePath = "policies/example-scope.yaml"): string {
  const lines: string[] = [];
  const approval = policy.approval;
  lines.push(`# Data scope policy: ${policy.agent_group} (version ${policy.version})`);
  lines.push("");
  lines.push(`Generated from \`${sourcePath}\`. Do not edit by hand; edit the policy and regenerate with \`npm run card\`.`);
  lines.push("");
  lines.push("## Status");
  lines.push("");
  lines.push("| Field | Value |");
  lines.push("|---|---|");
  lines.push(`| Approval status | ${approval?.status ?? "not recorded"} |`);
  lines.push(`| Owner | ${approval?.owner ?? "not recorded"} |`);
  lines.push(`| Approved by | ${approval?.approved_by ?? "pending"} |`);
  lines.push(`| Approved on | ${approval?.approved_on ?? "pending"} |`);
  lines.push(`| Classification scheme | ${policy.classification?.scheme ?? "none declared"} |`);
  lines.push("");
  lines.push("## Purpose");
  lines.push("");
  lines.push(policy.purpose.trim());
  lines.push("");
  if (policy.summary?.length) {
    lines.push("## The rules in plain English");
    lines.push("");
    policy.summary.forEach((rule, index) => lines.push(`${index + 1}. ${rule}`));
    lines.push("");
  }
  if (policy.classification) {
    lines.push("## Sensitivity tiers");
    lines.push("");
    lines.push("Tiers are declared per source by the agent's owner and approved here. The gate enforces and reports on the declaration; it does not guess sensitivity from content.");
    lines.push("");
    lines.push("| Tier | Meaning |");
    lines.push("|---|---|");
    for (const tier of policy.classification.tiers) lines.push(`| ${tier.label} | ${tier.meaning} |`);
    lines.push("");
  }
  lines.push("## What each agent may read");
  lines.push("");
  lines.push("| Agent | Source | Tier | Condition | Fields | On block |");
  lines.push("|---|---|---|---|---|---|");
  for (const [agent, block] of Object.entries(policy.agents)) {
    const onBlock = block.on_block ?? policy.defaults.on_block;
    for (const rule of block.permit) {
      lines.push(`| ${code(agent)} | ${code(rule.tool ?? rule.read ?? "?")} | ${rule.tier ?? "unclassified"} | ${rule.scope ? code(rule.scope) : "none"} | ${rule.fields ? rule.fields.join(", ") : "all returned"} | ${onBlock} |`);
    }
  }
  lines.push("");
  const expectations = Object.entries(policy.agents).flatMap(([agent, block]) =>
    (block.expect ?? []).flatMap((expectation) => Object.entries(expectation).map(([key, value]) => `- ${code(agent)}: ${key} ${code(value)}`)));
  if (expectations.length) {
    lines.push("## Expectations (scored after each run, not enforced)");
    lines.push("");
    lines.push(...expectations);
    lines.push("");
  }
  lines.push("## Forbidden everywhere");
  lines.push("");
  lines.push("These fields are stripped from every tool result, for every agent, before the model sees it.");
  lines.push("");
  for (const field of policy.forbid.fields_everywhere) lines.push(`- ${code(field)}`);
  lines.push("");
  if (policy.incidents) {
    const rules = policy.incidents;
    lines.push("## Incidents");
    lines.push("");
    lines.push("A gate event becomes an incident, with an owner and a state (open, acknowledged, remediated, closed), when:");
    lines.push("");
    lines.push(`- the gate blocks, drops, or strips on a source at tier ${rules.sensitive_tiers.join(" or ")} (nothing reached the model; something upstream drifted);`);
    lines.push("- a report cites evidence the gate never let through (an integrity flag);");
    lines.push(`- necessity scores below ${rules.necessity_floor} for ${rules.necessity_streak} runs in a row.`);
    lines.push("");
    const owners = Object.entries(policy.agents).filter(([, block]) => block.owner).map(([agent, block]) => `${code(agent)}: ${block.owner}`);
    lines.push(`Default owner: **${rules.owner}**${owners.length ? `. Per-agent owners: ${owners.join(", ")}` : ""}. Each run posts LangSmith feedback ${code(rules.feedback_key)} (1 when an incident opened, else 0); a LangSmith alert on that key notifies the owner.`);
    lines.push("");
  }
  lines.push("## Defaults");
  lines.push("");
  lines.push(`- Tools not listed for an agent: **${policy.defaults.unlisted_tools}**`);
  lines.push(`- Writes of any kind: **${policy.defaults.writes}**`);
  lines.push(`- When a call is blocked: **${policy.defaults.on_block}** the run, unless the agent's block says otherwise`);
  lines.push("");
  lines.push("## How this is enforced");
  lines.push("");
  lines.push("- A gate runs before and after every tool call in every agent and subagent. Permit and forbid are deterministic; no model decides.");
  lines.push("- Each decision is one evidence line (agent, tool, decision, rule, tier, record identifiers, counts) and one named step in the LangSmith trace, tagged with the policy version and tier. Evidence carries names and counts, never data.");
  lines.push("- Necessity is scored after each run as fetched-versus-cited and recorded as LangSmith feedback under `data_necessity_v1`.");
  lines.push("- A run that continued past a blocked read is marked as such and never reported as clean.");
  lines.push("");
  if (approval?.change_log.length) {
    lines.push("## Change log");
    lines.push("");
    lines.push("| Version | Date | Change |");
    lines.push("|---|---|---|");
    for (const entry of approval.change_log) lines.push(`| ${entry.version} | ${entry.on} | ${entry.change} |`);
    lines.push("");
  }
  return lines.join("\n");
}

export function policyCardIsCurrent(policy: ScopePolicy, cardPath: string, sourcePath?: string) {
  try { return readFileSync(cardPath, "utf8") === renderPolicyCard(policy, sourcePath); } catch { return false; }
}
