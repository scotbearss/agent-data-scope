"""Necessity scoring. Deterministic: no model is asked anything.

The gate records which record identifiers each agent was allowed to see
(fetched). Each agent's report cites the identifiers it relied on (cited).
Fetched and never cited is the first, cheapest evidence of "allowed but not
needed". Cited but never fetched is an integrity problem: the report points at
evidence the agent did not read through the gate.

Limits, stated plainly: a record can be needed without being cited (used to
rule something out), and citing everything would game the ratio. This is a
floor, not a verdict. A relevance map or a model judge can sit on top.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .gate import GateDecision

NECESSITY_FEEDBACK_KEY = "data_necessity_v1"


@dataclass(frozen=True)
class AgentNecessity:
    agent: str
    measurable: bool  # False when the agent's reads carry no identifiers
    fetched: tuple[str, ...]
    cited: tuple[str, ...]
    used: tuple[str, ...]  # fetched and cited
    unused: tuple[str, ...]  # fetched, never cited
    cited_not_fetched: tuple[str, ...]
    necessity: float | None  # used / fetched, or None when not measurable


@dataclass(frozen=True)
class OverallNecessity:
    fetched: int
    used: int
    unused: int
    necessity: float | None
    integrity: str  # "ok" | "cited_not_fetched"


@dataclass(frozen=True)
class NecessityScore:
    agents: tuple[AgentNecessity, ...]
    overall: OverallNecessity
    version: int = 1
    method: str = "fetched-vs-cited"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_necessity(decisions: Sequence[GateDecision], citations: Mapping[str, Sequence[str]]) -> NecessityScore:
    agents = sorted({d.agent for d in decisions} | set(citations.keys()))
    per_agent: list[AgentNecessity] = []
    for agent in agents:
        reads = [d for d in decisions if d.agent == agent and d.decision == "permit"]
        measurable = bool(reads) and any(d.record_refs is not None for d in reads)
        fetched = sorted({ref for d in reads for ref in (d.record_refs or ())})
        cited = sorted(set(citations.get(agent, ())))
        fetched_set, cited_set = set(fetched), set(cited)
        used = [ref for ref in fetched if ref in cited_set]
        unused = [ref for ref in fetched if ref not in cited_set]
        cited_not_fetched = [ref for ref in cited if ref not in fetched_set] if measurable else []
        per_agent.append(
            AgentNecessity(
                agent=agent,
                measurable=measurable,
                fetched=tuple(fetched),
                cited=tuple(cited),
                used=tuple(used),
                unused=tuple(unused),
                cited_not_fetched=tuple(cited_not_fetched),
                necessity=len(used) / len(fetched) if measurable and fetched else None,
            )
        )
    measured = [a for a in per_agent if a.measurable]
    fetched_total = sum(len(a.fetched) for a in measured)
    used_total = sum(len(a.used) for a in measured)
    return NecessityScore(
        agents=tuple(per_agent),
        overall=OverallNecessity(
            fetched=fetched_total,
            used=used_total,
            unused=fetched_total - used_total,
            necessity=used_total / fetched_total if fetched_total else None,
            integrity="cited_not_fetched" if any(a.cited_not_fetched for a in per_agent) else "ok",
        ),
    )


def necessity_feedback(score: NecessityScore) -> dict[str, Any]:
    """The LangSmith feedback row. Names and counts only."""
    unused = [f"{a.agent}: {ref}" for a in score.agents for ref in a.unused]
    o = score.overall
    return {
        "key": NECESSITY_FEEDBACK_KEY,
        "score": o.necessity,
        "comment": " ".join(
            [
                "Deterministic fetched-vs-cited check; not a human quality rating.",
                f"fetched {o.fetched}, used {o.used}, unused {o.unused}, integrity {o.integrity}.",
                f"Unused: {'; '.join(unused)}" if unused else "Nothing fetched went uncited.",
            ]
        ),
    }


def _retrieve_run(client: Any, run_id: str, project_id: str) -> Any:
    """``client.runs.retrieve`` is the current API; ``read_run`` is deprecated but still works."""
    runs = getattr(client, "runs", None)
    if runs is not None and hasattr(runs, "retrieve"):
        return runs.retrieve(run_id, project_id=project_id, selects=["ID", "STATUS"])
    return client.read_run(run_id, project_id=project_id)


def wait_for_run(client: Any, run_id: str, project_id: str, deadline_ms: int) -> int:
    """Feedback posted before LangSmith has ingested the run is accepted and silently dropped.

    Wait until the run is readable. Returns the milliseconds waited.
    """
    started = time.monotonic()
    while True:
        try:
            _retrieve_run(client, run_id, project_id)
            return int((time.monotonic() - started) * 1000)
        except Exception as error:  # noqa: BLE001 - any failure means "not readable yet"
            if (time.monotonic() - started) * 1000 > deadline_ms:
                raise RuntimeError(f"Run {run_id} was not readable within {deadline_ms}ms") from error
            time.sleep(2)


def record_necessity_feedback(client: Any, run_id: str, project_name: str, score: NecessityScore, deadline_ms: int = 60_000) -> dict[str, Any]:
    project = client.read_project(project_name=project_name)
    waited_ms = wait_for_run(client, run_id, str(project.id), deadline_ms)
    feedback = necessity_feedback(score)
    client.create_feedback(
        run_id,
        feedback["key"],
        score=feedback["score"],
        comment=feedback["comment"],
        feedback_source_type="api",
        project_id=project.id,
        extend_trace_retention=False,
    )
    return {**feedback, "waited_ms": waited_ms}
