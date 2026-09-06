# Data scope policy: example (version 2)

Generated from `policies/example-scope.yaml`. Do not edit by hand; edit the policy and regenerate with `npm run card`.

## Status

| Field | Value |
|---|---|
| Approval status | draft |
| Owner | platform-team |
| Approved by | pending |
| Approved on | pending |
| Classification scheme | PNI/BCI/SCI |

## Purpose

Investigate one operational incident and propose a fix for a human to approve. Draft a reply to one customer message for a human to review.

## The rules in plain English

1. The investigator exists to explain one incident and propose a fix for a human to approve. The support drafter exists to draft a reply to one customer message for a human to review.
2. The investigator may read the operational snapshot for the incident being investigated, and nothing from any other incident.
3. No agent may read message text, household records, names, email addresses, or phone numbers. If a tool ever returns such a field, it is stripped before the model sees it.
4. The investigator should read only records relevant to the incident's signals. Reading everything it is permitted to read is allowed, and is the behavior flagged as unnecessary.
5. The support drafter may read the subject and body of the one message it is replying to. Nothing else about that customer, and no other messages.
6. No agent may write, send, or change anything. All output goes to a human for review.

## Sensitivity tiers

Tiers are declared per source by the agent's owner and approved here. The gate enforces and reports on the declaration; it does not guess sensitivity from content.

| Tier | Meaning |
|---|---|
| PNI | Public non-private information |
| BCI | Business confidential information |
| SCI | Sensitive confidential information |

## What each agent may read

| Agent | Source | Tier | Condition | Fields | On block |
|---|---|---|---|---|---|
| `example-investigator` | `get_incident_snapshot` | BCI | `incident_id == incident.id` | all returned | stop |
| `example-support-drafter` | `get_incoming_message` | SCI | `message_id == case.message_id` | subject, body | stop |

## Expectations (scored after each run, not enforced)

- `example-investigator`: records_read `relevant_to(incident.signals)`

## Forbidden everywhere

These fields are stripped from every tool result, for every agent, before the model sees it.

- `message_text`
- `household_records`
- `person_name`
- `email_address`
- `phone_number`

## Incidents

A gate event becomes an incident, with an owner and a state (open, acknowledged, remediated, closed), when:

- the gate blocks, drops, or strips on a source at tier SCI (nothing reached the model; something upstream drifted);
- a report cites evidence the gate never let through (an integrity flag);
- necessity scores below 0.25 for 3 runs in a row.

Default owner: **platform-team**. Per-agent owners: `example-investigator`: ops-team. Each run posts LangSmith feedback `data_scope_incident_v1` (1 when an incident opened, else 0); a LangSmith alert on that key notifies the owner.

## Defaults

- Tools not listed for an agent: **block**
- Writes of any kind: **forbid**
- When a call is blocked: **stop** the run, unless the agent's block says otherwise

## How this is enforced

- A gate runs before and after every tool call in every agent and subagent. Permit and forbid are deterministic; no model decides.
- Each decision is one evidence line (agent, tool, decision, rule, tier, record identifiers, counts) and one named step in the LangSmith trace, tagged with the policy version and tier. Evidence carries names and counts, never data.
- Necessity is scored after each run as fetched-versus-cited and recorded as LangSmith feedback under `data_necessity_v1`.
- A run that continued past a blocked read is marked as such and never reported as clean.

## Change log

| Version | Date | Change |
|---|---|---|
| 1 | 2026-09-06 | First example policy, extracted from the sentinel build. |
| 2 | 2026-09-06 | LangSmith gateway adapter added; it derives a personal-data guard for agents that read sensitive-tier sources. No new policy fields. |
