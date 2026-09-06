# agent-data-scope (Python)

A governance layer for LangChain Deep Agents that proves an agent touched only the data its task needed. This is the Python port of the TypeScript framework in `../typescript`; both read the same policy file (`../policies/example-scope.yaml`) and write ledger and incident files with identical JSON keys, so either port can read the other's records.

Four parts:

1. **Policy** (`policy.py`): a YAML file with `permit` / `forbid` / `expect` rules, sensitivity tiers, an approval record and incident rules, validated once at load time.
2. **Gate** (`gate.py`): a middleware on every tool call that permits by name, drops out-of-scope records, strips forbidden fields, applies `on_block` (`stop` or `continue`), and writes one evidence line (names and counts, never data) plus one named trace step per decision.
3. **Necessity** (`necessity.py`): a deterministic fetched-versus-cited score, posted as LangSmith feedback `data_necessity_v1`.
4. **Incidents, fleet report, policy card** (`incidents.py`, `fleet_report.py`, `policy_card.py`): gate events on sensitive tiers become incidents with an owner and a state; the fleet report aggregates runs from LangSmith or a local ledger; the policy card is generated from the policy so the two cannot drift.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
cd python
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Run the tests

Fully offline: the network is blocked, and the "model" is a scripted fake.

```sh
uv run pytest -q
```

## Run the lesson

A fictional agent asks for one permitted tool and one it is not allowed to use. No model is paid for.

```sh
uv run agent-data-scope lesson              # scope drop, forbid strip, on_block continue, necessity 0.5
uv run agent-data-scope lesson --sci-probe  # adds an SCI source; the strip on it opens an incident
```

Without `LANGSMITH_API_KEY` the lesson runs untraced and prints the decisions, summary, mark, necessity score and incidents. With the key set, it traces to the LangSmith project `agent-data-scope-lesson` (override with `--project`) and posts `data_necessity_v1` and `data_scope_incident_v1` feedback on the run.

Every lesson run appends one record to `python/artifacts/data-scope-ledger.jsonl` and opens any incidents in `python/artifacts/data-scope-incidents.jsonl`.

## CLI

```sh
uv run agent-data-scope card                 # write python/example-scope.md from ../policies/example-scope.yaml
uv run agent-data-scope card --check         # exit 1 if the card is out of date
uv run agent-data-scope report --source ledger [--days 7] [--out report.md]
uv run agent-data-scope report --source langsmith --projects agent-data-scope-lesson   # needs LANGSMITH_API_KEY
uv run agent-data-scope incident             # list incidents
uv run agent-data-scope incident --id <id> --state acknowledged --note "looking"
```

The policy card for this port is written to `python/example-scope.md` (not `policies/example-scope.md`) so it does not collide with the TypeScript port's card.

## Using the gate in your own agent

```python
from deepagents import create_deep_agent
from agent_data_scope import create_data_scope_gate, create_scope_ledger, load_scope_policy

policy = load_scope_policy("policies/example-scope.yaml")
ledger = create_scope_ledger(policy)
gate = create_data_scope_gate(policy, "example-investigator", context={"incident": {"id": "inc-0001"}}, on_decision=ledger.record)
agent = create_deep_agent(model=..., tools=[...], middleware=[gate])
# after the run: ledger.decisions, ledger.summary(), ledger.mark()
```

A scoped permit whose context value is missing raises when the gate is built, not mid-run. In `stop` mode a blocked call raises `DataScopeStopError` out of `agent.invoke`; in `continue` mode the model receives an error `ToolMessage` and the run is marked as completed with blocked reads.
