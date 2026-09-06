import { INCIDENT_STATES, readIncidents, transitionIncident, type IncidentState } from "./incidents.js";

/**
 * npm run incident                        list incidents
 * npm run incident -- --id <id> --state acknowledged --note "looking"
 * Moves an incident forward: open -> acknowledged -> remediated -> closed.
 */
const PATH = "runs/incidents.jsonl";
const args = new Map<string, string>();
for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index].replace(/^--/, ""), process.argv[index + 1] ?? "");
const id = args.get("id");
if (!id) {
  for (const incident of readIncidents(PATH)) console.log(`${incident.state.padEnd(12)} ${incident.type.padEnd(22)} ${incident.agent.padEnd(40)} owner ${incident.owner.padEnd(10)} ${incident.id}`);
} else {
  const state = args.get("state") as IncidentState | undefined;
  if (!state || !INCIDENT_STATES.includes(state)) { console.error(`--state must be one of ${INCIDENT_STATES.join(", ")}`); process.exit(1); }
  const updated = transitionIncident(PATH, id, state, args.get("note"));
  console.log(`${updated.id} -> ${updated.state}`);
}
