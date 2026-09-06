import assert from "node:assert/strict";
import { createDataScopeGate, DataScopeStopError, loadScopePolicy } from "../src/gate.js";
import { createDataScopeLessonAgent, runDataScopeLesson, scriptedLessonModel, LESSON_AGENT, LESSON_INCIDENT } from "../src/lesson.js";
import { necessityFeedback, scoreNecessity } from "../src/necessity.js";

globalThis.fetch = async () => { throw new Error("Offline only"); };

// The real policy file loads and validates.
const policy = loadScopePolicy("../policies/example-scope.yaml");
assert.equal(policy.version, 2);
assert.deepEqual(policy.classification?.tiers.map((tier) => tier.label), ["PNI", "BCI", "SCI"]);
assert.equal(policy.approval?.status, "draft");

// Step six: tiers are validated against the organization's classification, and the card is current.
{
  const { ScopePolicy, tierRank, highestTier } = await import("../src/gate.js");
  const { renderPolicyCard, policyCardIsCurrent } = await import("../src/policy-card.js");
  const wrongTier = structuredClone(policy) as typeof policy;
  wrongTier.agents["example-support-drafter"].permit[0].tier = "TOP_SECRET";
  assert.throws(() => ScopePolicy.parse(wrongTier), /tier TOP_SECRET is not in the classification/);
  const missingTier = structuredClone(policy) as typeof policy;
  delete missingTier.agents["example-support-drafter"].permit[0].tier;
  assert.throws(() => ScopePolicy.parse(missingTier), /example-support-drafter.get_incoming_message: missing tier/);
  assert.equal(tierRank(policy, "SCI"), 2);
  assert.equal(highestTier(policy, [{ tier: "PNI" }, { tier: "BCI" }, {}]), "BCI");
  const card = renderPolicyCard(policy);
  assert.match(card, /^# Data scope policy: example \(version 2\)/);
  assert.match(card, /\| Approval status \| draft \|/);
  assert.match(card, /\| `example-support-drafter` \| `get_incoming_message` \| SCI \| `message_id == case.message_id` \| subject, body \| stop \|/);
  assert.match(card, /1\. The investigator exists/);
  assert.ok(policyCardIsCurrent(policy, "../policies/example-scope.md"), "../policies/example-scope.md is generated from the policy; run npm run scope:card");
}
assert.equal(policy.defaults.unlisted_tools, "block");
assert.equal(policy.defaults.on_block, "stop");
assert.ok(policy.agents[LESSON_AGENT]);

// An agent with no block in the policy cannot even be built.
assert.throws(() => createDataScopeGate({ policy, agent: "example-unknown" }), /no block for agent/);
// A scoped permit without its context value cannot be built either: fail at build time, not mid-run.
assert.throws(() => createDataScopeGate({ policy, agent: LESSON_AGENT }), /needs context incident.id/);

// Default on_block is stop: the blocked read ends the run, and the blocked tool never ran.
{
  const session = createDataScopeLessonAgent(scriptedLessonModel());
  // The framework wraps errors thrown from middleware; the original is the cause.
  const unwrap = (error: unknown): DataScopeStopError | undefined =>
    error instanceof DataScopeStopError ? error
      : error && typeof error === "object" && "cause" in error ? unwrap((error as { cause?: unknown }).cause) : undefined;
  await assert.rejects(runDataScopeLesson(session), (error: unknown) => {
    const stop = unwrap(error);
    return !!stop && /get_household_records/.test(stop.message) && stop.gateDecision.rule === "defaults.unlisted_tools";
  });
  assert.equal(session.ran.forbiddenTool, 0, "the blocked tool never ran");
  assert.ok(session.ledger.decisions.some((decision) => decision.decision === "block"));
}

// The agent's owner chose continue: the model gets a refusal, the run finishes, and it is marked.
{
  const session = createDataScopeLessonAgent(scriptedLessonModel(), { onBlock: "continue" });
  const outcome = await runDataScopeLesson(session);

  assert.equal(outcome.decisions.length, 2, "the gate decided on both tool calls");
  const permit = outcome.decisions.find((decision) => decision.tool === "get_incident_snapshot")!;
  const block = outcome.decisions.find((decision) => decision.tool === "get_household_records")!;
  assert.equal(permit.decision, "permit");
  assert.equal(permit.rule, `agents.${LESSON_AGENT}.permit`);
  assert.equal(permit.tier, "BCI", "the decision carries the declared tier");
  assert.equal(block.tier, undefined, "an unlisted tool has no declared tier");
  assert.equal(outcome.summary.highestTier, "BCI");
  assert.equal(permit.recordsChecked, 3, "all records were checked against the incident");
  assert.equal(permit.recordsDropped, 1, "the record from another case was dropped");
  assert.deepEqual(permit.strippedFields, ["email_address"], "the forbidden field was stripped");
  assert.equal(block.decision, "block");
  assert.equal(block.rule, "defaults.unlisted_tools");
  assert.equal(outcome.ran.permittedTool, 1);
  assert.equal(outcome.ran.forbiddenTool, 0);

  // What the model actually saw: two records, right case, no forbidden field.
  const seen = outcome.toolMessages.find((message) => message.name === "get_incident_snapshot")!;
  const body = JSON.parse(String(seen.content)) as { records: Array<Record<string, unknown>> };
  assert.equal(body.records.length, 2);
  assert.ok(body.records.every((record) => record.incidentId === LESSON_INCIDENT));
  assert.ok(!("email_address" in body.records[0]));
  assert.deepEqual(permit.recordRefs, ["snapshot:fictional:signals", "snapshot:fictional:heartbeat"], "evidence names the records the model saw");
  assert.ok(!String(seen.content).includes("example.invalid"), "the stripped value is gone");

  const refused = outcome.toolMessages.find((message) => message.name === "get_household_records")!;
  assert.match(String(refused.content), /Blocked by data scope policy v2/);
  assert.match(outcome.finalText, /Fictional report/);

  // Step five: deterministic necessity. Two records were seen, one was cited.
  assert.deepEqual(outcome.citedRefs, ["snapshot:fictional:signals"]);
  const scored = outcome.necessity.agents.find((agent) => agent.agent === LESSON_AGENT)!;
  assert.equal(scored.measurable, true);
  assert.deepEqual(scored.used, ["snapshot:fictional:signals"]);
  assert.deepEqual(scored.unused, ["snapshot:fictional:heartbeat"], "fetched and never cited");
  assert.deepEqual(scored.citedNotFetched, []);
  assert.equal(scored.necessity, 0.5);
  assert.deepEqual(outcome.necessity.overall, { fetched: 2, used: 1, unused: 1, necessity: 0.5, integrity: "ok" });
  const feedback = necessityFeedback(outcome.necessity);
  assert.equal(feedback.key, "data_necessity_v1");
  assert.equal(feedback.score, 0.5);
  assert.match(feedback.comment, /Unused: example-investigator: snapshot:fictional:heartbeat/);
  assert.ok(!feedback.comment.includes("fresh") && !feedback.comment.includes("example.invalid"), "feedback carries names, never data");

  // Integrity: a report that cites something the gate never let through is flagged.
  const tampered = scoreNecessity(outcome.decisions, new Map([[LESSON_AGENT, ["snapshot:fictional:signals", "snapshot:fictional-other:platform"]]]));
  assert.equal(tampered.overall.integrity, "cited_not_fetched");
  assert.deepEqual(tampered.agents[0].citedNotFetched, ["snapshot:fictional-other:platform"]);
  assert.equal(outcome.mark, "completed with 1 blocked read, 1 record dropped, 1 field stripped");
  assert.deepEqual(outcome.summary, { highestTier: "BCI", permitted: 1, blocked: 1, recordsDropped: 1, fieldsStripped: 1 });

  // Evidence lines carry names and counts only, never values. A record identifier
  // may contain the word "heartbeat"; the heartbeat's value "fresh" must never appear.
  const evidence = JSON.stringify(outcome.decisions);
  assert.ok(evidence.includes("snapshot:fictional:heartbeat"), "record identifiers are evidence");
  assert.ok(!evidence.includes("fresh") && !evidence.includes("example.invalid") && !evidence.includes("delivery_failure"), "values never are");
}
console.log("Data scope gate passed: scope drops foreign records, forbid strips fields, stop ends the run, continue marks it, necessity is scored without a model.");
