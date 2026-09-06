import { writeFileSync } from "node:fs";
import { loadScopePolicy } from "./gate.js";
import { buildFleetReport, langSmithDecisionSource, ledgerDecisionSource, renderFleetReport } from "./fleet-report.js";
import { readIncidents } from "./incidents.js";
import { traceClient } from "./tracing.js";

/**
 * npm run report -- --source langsmith --projects agent-data-scope-lesson --days 7 [--out path]
 * npm run report -- --source ledger --ledger runs/ledger.jsonl --days 7
 * Reads only. No model is called.
 */
const args = new Map<string, string>();
for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index].replace(/^--/, ""), process.argv[index + 1] ?? "");
const days = Number(args.get("days") ?? "7");
const until = new Date();
const since = new Date(until.getTime() - days * 24 * 60 * 60 * 1000);
const policy = loadScopePolicy("../policies/example-scope.yaml");

const sourceName = args.get("source") ?? "langsmith";
let source;
if (sourceName === "ledger") {
  source = ledgerDecisionSource(args.get("ledger") ?? "runs/ledger.jsonl");
} else {
  const key = process.env.LANGSMITH_API_KEY;
  if (!key) { console.error("LANGSMITH_API_KEY is not set. Export it, or use --source ledger."); process.exit(1); }
  source = langSmithDecisionSource(traceClient(key), (args.get("projects") ?? "agent-data-scope-lesson").split(","));
}
const records = await source.runs({ since, until });
const incidents = readIncidents(args.get("incidents") ?? "runs/incidents.jsonl");
const markdown = renderFleetReport(buildFleetReport(records, source.name, { since, until }, policy, incidents));
const out = args.get("out");
if (out) { writeFileSync(out, markdown); console.log(`Wrote ${out} (${records.length} runs).`); } else console.log(markdown);
