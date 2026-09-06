"""The policy card. Governance reads this, not the YAML and not the traces.

It is generated from the policy file so the two cannot drift: the plain-English
summary on top, the enforced rules underneath, sensitivity tiers per source, and
the approval record.
"""

from __future__ import annotations

from .policy import ScopePolicy

DEFAULT_POLICY_LABEL = "policies/example-scope.yaml"
DEFAULT_REGENERATE_COMMAND = "uv run agent-data-scope card"


def _code(text: str) -> str:
    return f"`{text}`"


def render_policy_card(policy: ScopePolicy, policy_label: str = DEFAULT_POLICY_LABEL, regenerate_command: str = DEFAULT_REGENERATE_COMMAND) -> str:
    lines: list[str] = []
    approval = policy.approval
    lines.append(f"# Data scope policy: {policy.agent_group} (version {policy.version})")
    lines.append("")
    lines.append(f"Generated from `{policy_label}`. Do not edit by hand; edit the policy and regenerate with `{regenerate_command}`.")
    lines.append("")
    lines.append("## Status")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Approval status | {approval.status if approval else 'not recorded'} |")
    lines.append(f"| Owner | {approval.owner if approval else 'not recorded'} |")
    lines.append(f"| Approved by | {(approval.approved_by if approval else None) or 'pending'} |")
    lines.append(f"| Approved on | {(approval.approved_on if approval else None) or 'pending'} |")
    lines.append(f"| Classification scheme | {policy.classification.scheme if policy.classification else 'none declared'} |")
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.append(policy.purpose.strip())
    lines.append("")
    if policy.summary:
        lines.append("## The rules in plain English")
        lines.append("")
        for index, rule in enumerate(policy.summary, start=1):
            lines.append(f"{index}. {rule}")
        lines.append("")
    if policy.classification:
        lines.append("## Sensitivity tiers")
        lines.append("")
        lines.append("Tiers are declared per source by the agent's owner and approved here. The gate enforces and reports on the declaration; it does not guess sensitivity from content.")
        lines.append("")
        lines.append("| Tier | Meaning |")
        lines.append("|---|---|")
        for tier in policy.classification.tiers:
            lines.append(f"| {tier.label} | {tier.meaning} |")
        lines.append("")
    lines.append("## What each agent may read")
    lines.append("")
    lines.append("| Agent | Source | Tier | Condition | Fields | On block |")
    lines.append("|---|---|---|---|---|---|")
    for agent, block in policy.agents.items():
        on_block = block.on_block or policy.defaults.on_block
        for rule in block.permit:
            lines.append(
                f"| {_code(agent)} | {_code(rule.tool or rule.read or '?')} | {rule.tier or 'unclassified'} | {_code(rule.scope) if rule.scope else 'none'} | {', '.join(rule.fields) if rule.fields else 'all returned'} | {on_block} |"
            )
    lines.append("")
    expectations = [f"- {_code(agent)}: {key} {_code(value)}" for agent, block in policy.agents.items() for expectation in (block.expect or []) for key, value in expectation.items()]
    if expectations:
        lines.append("## Expectations (scored after each run, not enforced)")
        lines.append("")
        lines.extend(expectations)
        lines.append("")
    lines.append("## Forbidden everywhere")
    lines.append("")
    lines.append("These fields are stripped from every tool result, for every agent, before the model sees it.")
    lines.append("")
    for field in policy.forbid.fields_everywhere:
        lines.append(f"- {_code(field)}")
    lines.append("")
    if policy.incidents:
        rules = policy.incidents
        lines.append("## Incidents")
        lines.append("")
        lines.append("A gate event becomes an incident, with an owner and a state (open, acknowledged, remediated, closed), when:")
        lines.append("")
        lines.append(f"- the gate blocks, drops, or strips on a source at tier {' or '.join(rules.sensitive_tiers)} (nothing reached the model; something upstream drifted);")
        lines.append("- a report cites evidence the gate never let through (an integrity flag);")
        lines.append(f"- necessity scores below {rules.necessity_floor} for {rules.necessity_streak} runs in a row.")
        lines.append("")
        owners = [f"{_code(agent)}: {block.owner}" for agent, block in policy.agents.items() if block.owner]
        per_agent = f". Per-agent owners: {', '.join(owners)}" if owners else ""
        lines.append(f"Default owner: **{rules.owner}**{per_agent}. Each run posts LangSmith feedback {_code(rules.feedback_key)} (1 when an incident opened, else 0); a LangSmith alert on that key notifies the owner.")
        lines.append("")
    lines.append("## Defaults")
    lines.append("")
    lines.append(f"- Tools not listed for an agent: **{policy.defaults.unlisted_tools}**")
    lines.append(f"- Writes of any kind: **{policy.defaults.writes}**")
    lines.append(f"- When a call is blocked: **{policy.defaults.on_block}** the run, unless the agent's block says otherwise")
    lines.append("")
    lines.append("## How this is enforced")
    lines.append("")
    lines.append("- A gate runs before and after every tool call in every agent and subagent. Permit and forbid are deterministic; no model decides.")
    lines.append("- Each decision is one evidence line (agent, tool, decision, rule, tier, record identifiers, counts) and one named step in the LangSmith trace, tagged with the policy version and tier. Evidence carries names and counts, never data.")
    lines.append("- Necessity is scored after each run as fetched-versus-cited and recorded as LangSmith feedback under `data_necessity_v1`.")
    lines.append("- A run that continued past a blocked read is marked as such and never reported as clean.")
    lines.append("")
    if approval and approval.change_log:
        lines.append("## Change log")
        lines.append("")
        lines.append("| Version | Date | Change |")
        lines.append("|---|---|---|")
        for entry in approval.change_log:
            lines.append(f"| {entry.version} | {entry.on} | {entry.change} |")
        lines.append("")
    return "\n".join(lines)


def policy_card_is_current(policy: ScopePolicy, card_path: str, policy_label: str = DEFAULT_POLICY_LABEL, regenerate_command: str = DEFAULT_REGENERATE_COMMAND) -> bool:
    try:
        with open(card_path, encoding="utf-8") as handle:
            return handle.read() == render_policy_card(policy, policy_label, regenerate_command)
    except OSError:
        return False
