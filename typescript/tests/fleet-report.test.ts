import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadScopePolicy } from "../src/gate.js";
import {
  appendFleetRunRecord, buildFleetReport, integrityFromComment, ledgerDecisionSource,
  parseSummaryStep, policyVersionFromTags, renderFleetReport, type FleetRunRecord,
} from "../src/fleet-report.js";

globalThis.fetch = async () => { throw new Error("Offline only"); };
const policy = loadScopePolicy("../policies/example-scope.yaml");

// The gate's summary step names are the wire format. They must parse exactly.
assert.deepEqual(parseSummaryStep("DataScopeGate summary example-investigator | permitted 1 | blocked 1 | dropped 1 | stripped 1 | highest tier BCI"),
  { agent: "example-investigator", permitted: 1, blocked: 1, dropped: 1, stripped: 1, highestTier: "BCI" });
assert.deepEqual(parseSummaryStep("DataScopeGate summary lead | permitted 2 | blocked 0 | dropped 0 | stripped 0"),
  { agent: "lead", permitted: 2, blocked: 0, dropped: 0, stripped: 0 });
assert.equal(parseSummaryStep("DataScopeGate permit get_incident_snapshot | dropped 1"), undefined);
assert.equal(policyVersionFromTags(["lesson", "policy-v3", "tier-BCI"]), 3);
assert.equal(policyVersionFromTags(["lesson"]), null);
assert.equal(integrityFromComment("fetched 2, used 1, unused 1, integrity ok."), "ok");
assert.equal(integrityFromComment("integrity cited_not_fetched."), "cited_not_fetched");
assert.equal(integrityFromComment(undefined), "unknown");

// A small fleet: two clean investigation runs, one lesson run where the gate acted.
const clean = (runId: string, startedAt: string): FleetRunRecord => ({
  runId, runName: "Example investigation", startedAt, policyVersion: 1, necessity: 0.6, integrity: "ok",
  agents: ["example-investigator", "example-support-drafter"]
    .map((agent) => ({ agent, permitted: 1, blocked: 0, dropped: 0, stripped: 0, highestTier: agent.endsWith("drafter") ? "SCI" : "BCI" })),
});
const acted: FleetRunRecord = {
  runId: "33333333-3333-4333-8333-333333333333", runName: "Data scope gate lesson", startedAt: "2026-09-06T20:00:00.000Z", policyVersion: 1, necessity: 0.5, integrity: "ok",
  agents: [{ agent: "example-investigator", permitted: 1, blocked: 1, dropped: 1, stripped: 1, highestTier: "BCI" }],
};
const records = [clean("11111111-1111-4111-8111-111111111111", "2026-09-05T10:00:00.000Z"), clean("22222222-2222-4222-8222-222222222222", "2026-09-06T10:00:00.000Z"), acted];
const period = { since: new Date("2026-09-01T00:00:00Z"), until: new Date("2026-09-07T00:00:00Z") };
const report = buildFleetReport(records, "test", period, policy);
assert.deepEqual(report.policyVersions, [1]);
assert.equal(report.totals.runs, 3);
assert.equal(report.totals.clean, 2);
assert.equal(report.totals.gateActed, 1);
assert.deepEqual([report.totals.blocked, report.totals.dropped, report.totals.stripped], [1, 1, 1]);
assert.equal(report.totals.necessityScored, 3);
assert.ok(Math.abs((report.totals.necessityAverage ?? 0) - (0.6 + 0.6 + 0.5) / 3) < 1e-9);
assert.equal(report.totals.integrityFlags, 0);
assert.equal(report.totals.highestTier, "SCI");
const platform = report.agents.find((agent) => agent.agent === "example-investigator")!;
assert.deepEqual([platform.runs, platform.clean, platform.blocked], [3, 2, 1]);
assert.equal(report.gateActedRuns.length, 1);
assert.equal(report.gateActedRuns[0].runId, acted.runId);

const markdown = renderFleetReport(report);
assert.match(markdown, /\| 3 \| 2 \(67%\) \| 1 \(33%\) \| 1 \| 1 \| 1 \| 0\.57 over 3 \| 0 \| SCI \|/);
assert.match(markdown, /`example-investigator` \| 3 \| 2 \(67%\) \| 1 \| 1 \| 1 \| BCI/);
assert.match(markdown, /Data scope gate lesson \(`33333333`\)/);
assert.ok(!markdown.includes("fresh") && !markdown.includes("example.invalid"), "the report carries counts, never data");
assert.match(markdown, /Open 0, acknowledged 0, remediated 0, closed 0/);
assert.match(markdown, /None on file\./);

// The ledger source round-trips records and honors the period.
const dir = mkdtempSync(join(tmpdir(), "scope-ledger-"));
try {
  const path = join(dir, "ledger.jsonl");
  for (const record of records) appendFleetRunRecord(path, record);
  const source = ledgerDecisionSource(path);
  const all = await source.runs(period);
  assert.deepEqual(all.map((record) => record.runId), [acted.runId, records[1].runId, records[0].runId], "newest first");
  const later = await source.runs({ since: new Date("2026-09-06T00:00:00Z"), until: period.until });
  assert.equal(later.length, 2);
  assert.deepEqual(await ledgerDecisionSource(join(dir, "missing.jsonl")).runs(period), []);
} finally { rmSync(dir, { recursive: true, force: true }); }
console.log("Fleet report passed: summary steps parse, totals and per-agent rows aggregate, ledger source round-trips, no data in the report.");
