"""Delegation tool — spawn child agents."""

from __future__ import annotations

import json
import concurrent.futures
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import cfg_get
from gyrfalcon.tools.restrictions import clamp_toolsets

logger = get_logger("tools.delegate")


def delegate_task(args: dict, **kwargs) -> str:
    """Spawn child AIAgent(s) with isolated context."""
    logger.debug("Beginning of delegate_task")
    goal = args.get("goal")
    context = args.get("context", "")
    toolsets = args.get("toolsets")
    tasks = args.get("tasks")  # Batch mode
    max_iterations = args.get("max_iterations", 30)
    role = args.get("role", "leaf")

    if not goal and not tasks:
        return json.dumps({"error": "Either 'goal' or 'tasks' is required"})

    # Config limits
    max_concurrent = cfg_get("delegation.max_concurrent_children", 3)
    max_depth = cfg_get("delegation.max_spawn_depth", 2)

    # Leaf agents cannot delegate further
    leaf_disabled_toolsets = ["delegation"] if role == "leaf" else None

    # A restricted caller (a chat-platform agent) cannot hand a child more than
    # it holds. Without this, `toolsets` — which is model-supplied — or its
    # absence, which means the full default set including the shell, would be a
    # way around the ceiling with a single tool call. The child is itself
    # restricted, so the clamp holds through further nesting.
    allowed = kwargs.get("allowed_tools")
    restricted = allowed is not None
    toolsets = clamp_toolsets(toolsets, allowed)

    from gyrfalcon.run_agent import AIAgent

    if tasks:
        # Batch parallel mode
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures = {}
            for i, task in enumerate(tasks[:max_concurrent * 2]):
                task_goal = task if isinstance(task, str) else task.get("goal", "")
                task_context = context if isinstance(task, str) else task.get("context", context)

                agent = AIAgent(
                    model=kwargs.get("model", cfg_get("model.name", "")),
                    base_url=kwargs.get("base_url"),
                    api_key=kwargs.get("api_key"),
                    max_iterations=max_iterations,
                    enabled_toolsets=toolsets,
                    disabled_toolsets=leaf_disabled_toolsets,
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    platform="delegation",
                    restrict_tools=restricted,
                )

                prompt = f"Task: {task_goal}"
                if task_context:
                    prompt = f"Context: {task_context}\n\n{prompt}"

                future = executor.submit(agent.chat, prompt)
                futures[future] = i

            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result(timeout=300)
                    results.append({"index": idx, "result": result})
                except Exception as e:
                    results.append({"index": idx, "error": str(e)})

        results.sort(key=lambda x: x["index"])
        return json.dumps({"results": results, "mode": "batch"})

    else:
        # Single task mode
        agent = AIAgent(
            model=kwargs.get("model", cfg_get("model.name", "")),
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
            max_iterations=max_iterations,
            enabled_toolsets=toolsets,
            disabled_toolsets=leaf_disabled_toolsets,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="delegation",
            restrict_tools=restricted,
        )

        prompt = f"Task: {goal}"
        if context:
            prompt = f"Context: {context}\n\n{prompt}"

        try:
            result = agent.chat(prompt)
            return json.dumps({"result": result, "mode": "single"})
        except Exception as e:
            return json.dumps({"error": str(e)})


# Register tool
registry.register(
    name="delegate_task",
    toolset="delegation",
    schema={
        "name": "delegate_task",
        "description": "Spawn child AI agent(s) to handle subtasks. Use for complex tasks that benefit from isolated context or parallel execution.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Task goal for single delegation"},
                "context": {"type": "string", "description": "Context/background for the task"},
                "toolsets": {"type": "array", "items": {"type": "string"}, "description": "Toolsets to enable"},
                "tasks": {"type": "array", "items": {"type": "object"}, "description": "Batch tasks for parallel execution"},
                "max_iterations": {"type": "integer", "description": "Max iterations for child agent", "default": 30},
                "role": {"type": "string", "enum": ["leaf", "orchestrator"], "default": "leaf"},
            },
        },
    },
    handler=delegate_task,
    emoji="🔀",
)
