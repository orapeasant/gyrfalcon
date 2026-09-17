"""Job search flow — a LangGraph node run as a flow activity (task).

This is a deliberately small proof-of-concept for one question: **can a
LangGraph node be the body of a Gyrfalcon task**, so a deterministic flow can
call into a graph-shaped piece of logic the same way it calls any other
activity? The answer here is yes, and the pattern is the whole point of the
file — everything else (the query, the search backend) is filler.

Shape:

    fde_job_search        (@flow)   — the deterministic outer flow
      └─ run_job_search_graph  (@task)   — the "activity": invokes a compiled
                                            LangGraph graph and returns its
                                            final state
           └─ search_jobs        (LangGraph node) — does the actual web search

Only the task boundary talks to the flow engine; the graph inside it is
ordinary LangGraph and knows nothing about Gyrfalcon. That's the intended
seam: retries/timeout/caching live on the task (see `run_job_search_graph`
below), while the graph itself stays swappable for a bigger multi-node
LangGraph pipeline later without touching the flow.

Requires `langgraph` (`uv pip install langgraph`) — imported lazily inside
`_build_graph()` so this module still imports cleanly (e.g. for
documentation tooling) without the dependency installed; only *running* the
flow needs it.

The web search itself reuses Gyrfalcon's own `web_search` tool
(`gyrfalcon/tools/web_tools.py`), which already knows how to pick a
configured backend (Exa / Tavily / Firecrawl) from `.env` — no separate
LangChain search-tool wiring or API key needed beyond what Gyrfalcon already
supports.

Run directly for a quick smoke test:

    uv run python sample/flow/job/fde_job_search_flow.py

Or drop this file (or a symlink to it) in `~/.gyrfalcon/flows/` to register
`fde_job_search` as a deployable flow, the same way any other flow file in
that directory is picked up on import.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from gyrfalcon.flow import flow, task

logger = logging.getLogger("gyrfalcon.flow.job")

#: The role this sample searches for. Kept as a module constant rather than
#: hardcoded in the query string so a copy of this file for a different role
#: is a one-line change.
DEFAULT_QUERY = "AI Forward Deployment Engineer (FDE) jobs"


class JobSearchState(TypedDict):
    """LangGraph state — plain dict-like, carried through every node."""

    query: str
    limit: int
    raw_results: str
    jobs: list[dict[str, Any]]


# ── The LangGraph node ───────────────────────────────────────────────────────
#
# A LangGraph node is just a callable: state in, partial (or full) state out.
# This one does the whole job in a single node on purpose — the sample is
# about the *activity boundary*, not about graph topology. A real pipeline
# would add nodes for e.g. de-duplication or LLM-based relevance filtering
# between `search_jobs` and the end of the graph.

def search_jobs(state: JobSearchState) -> JobSearchState:
    """Web-search node: queries Gyrfalcon's configured search backend."""
    from gyrfalcon.tools.web_tools import web_search

    raw = web_search({"query": state["query"], "limit": state["limit"]})
    parsed: dict[str, Any]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = {}

    if "error" in parsed:
        logger.warning("web_search returned an error: %s", parsed["error"])

    return {
        **state,
        "raw_results": raw,
        "jobs": parsed.get("results", []),
    }


def _build_graph():
    """Compiles the single-node graph. Imported lazily so this module loads
    without `langgraph` installed; only calling the task needs it."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError(
            "sample/flow/job/fde_job_search_flow.py needs langgraph — "
            "install it with `uv pip install langgraph`"
        ) from exc

    graph = StateGraph(JobSearchState)
    graph.add_node("search_jobs", search_jobs)
    graph.add_edge(START, "search_jobs")
    graph.add_edge("search_jobs", END)
    return graph.compile()


# ── The activity ─────────────────────────────────────────────────────────────
#
# The compiled graph is invoked *inside* a `@task`. Retries and the delay live
# here, on the activity, exactly like `fetch_page` in the quickstart sample:
# a flaky search API burns one task retry, not a full re-run of the flow.

@task(retries=2, retry_delay_seconds=5)
def run_job_search_graph(query: str, limit: int) -> JobSearchState:
    """Runs the LangGraph graph to completion and returns its final state."""
    app = _build_graph()
    return app.invoke({"query": query, "limit": limit, "raw_results": "", "jobs": []})


# ── The flow ─────────────────────────────────────────────────────────────────

@flow(name="fde_job_search", timeout_seconds=120)
def fde_job_search(query: str = DEFAULT_QUERY, limit: int = 10) -> dict[str, Any]:
    """Searches for AI Forward Deployment Engineer job postings.

    Kept as a thin flow around one activity call so the flow's job is purely
    orchestration — parameters in, activity result shaped for the caller out.
    """
    result = run_job_search_graph(query, limit)
    jobs = result.get("jobs", [])
    return {"query": query, "count": len(jobs), "jobs": jobs}


if __name__ == "__main__":
    output = fde_job_search()
    print(json.dumps(output, indent=2))
