"""Subagents.

delegate     — split work into independent subtasks; each runs in a fresh
               agent with its own context (keeps the parent's context small,
               which matters a lot for 4B-9B local models).
contemplate  — several agents attack the same hard question from different
               angles, then a synthesizer merges the best parts.

Parallelism is capped by agent.max_parallel_subagents. Ollama only runs
requests concurrently if OLLAMA_NUM_PARALLEL > 1, so on the 8 GB laptop
profile they run one after another.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import tool, truncate

PERSPECTIVES = [
    ("first-principles analyst", "Reason from first principles. Verify facts with tools before relying on them."),
    ("skeptical critic", "Look for what could be wrong, missing, or risky. Test assumptions; check edge cases."),
    ("pragmatic implementer", "Find the simplest approach that actually works. Prefer concrete, tested steps."),
    ("researcher", "Gather evidence from the web and files. Cite where each key fact came from."),
    ("creative explorer", "Consider unconventional approaches others would miss, then sanity-check them."),
]


def _parent(rt):
    return getattr(rt.local, "agent", None)


def _spawn(rt, task: str, persona: str = "", context: str = "") -> str:
    from ..agent import Agent
    parent = _parent(rt)
    depth = (parent.depth + 1) if parent else 1
    mode = parent.mode if parent else "auto"
    max_depth = rt.cfg.agent["max_subagent_depth"]
    sub = Agent(rt, mode=mode, depth=depth,
                system_extra=(f"You are a subagent ({persona}). " if persona else "You are a subagent. ")
                + "Complete ONLY the task below and reply with a complete, self-contained result. "
                  "The parent cannot see your tool calls — include every finding, file path and number it needs."
                + (f"\n\nContext from the parent agent:\n{context}" if context else ""),
                exclude_tools={"delegate", "contemplate"} if depth >= max_depth else None,
                on_event=(lambda kind, data: rt.emit(kind, {**data, "subagent": depth})) if rt.on_event else None)
    try:
        result = sub.run(task)
    except Exception as e:
        return f"[subagent failed: {e}]"
    finally:
        rt.local.agent = parent  # the subagent ran on this thread too
    if sub.taint and parent is not None:
        parent.taint |= {f"{t} (via a helper)" for t in sub.taint}
    return result


@tool("delegate", "Run independent subtasks in parallel subagents, each with a fresh context. Use for work that "
      "splits cleanly (research 4 products, summarize 6 files, build 3 separate modules). Returns every result.",
      {"tasks": {"type": "array", "items": {"type": "string"}, "description": "self-contained task descriptions"},
       "context": {"type": "string", "description": "shared background every subagent needs"}}, ["tasks"], group="subagents")
def delegate(rt, tasks: list, context: str = "") -> str:
    if isinstance(tasks, str):
        tasks = [t.strip("-• ").strip() for t in tasks.splitlines() if t.strip()]
    tasks = [str(t) for t in tasks][:8]
    if not tasks:
        return "ERROR: no tasks given"
    workers = max(1, int(rt.cfg.agent["max_parallel_subagents"]))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda t: _spawn(rt, t, context=context), tasks))
    per = max(1500, rt.cfg.agent["max_tool_output_chars"] // max(1, len(tasks)))
    return "\n\n".join(f"=== subtask {i}: {t[:120]}\n{truncate(r, per)}" for i, (t, r) in enumerate(zip(tasks, results), 1))


def contemplate_run(rt, question: str, n: int = 3) -> str:
    n = max(2, min(int(n or 3), len(PERSPECTIVES)))
    chosen = PERSPECTIVES[:n]
    workers = max(1, int(rt.cfg.agent["max_parallel_subagents"]))
    rt.emit("status", {"text": f"contemplating with {n} independent solvers"})
    with ThreadPoolExecutor(max_workers=workers) as ex:
        drafts = list(ex.map(lambda p: _spawn(rt, f"{question}\n\nApproach: {p[1]}", persona=p[0]), chosen))
    rt.emit("status", {"text": "synthesizing"})
    body = "\n\n".join(f"### Draft {i} ({p[0]})\n{d}" for i, (p, d) in enumerate(zip(chosen, drafts), 1))
    synth_p = rt.router.deep()
    synth = synth_p.chat([
        {"role": "system", "content": "You merge independent expert drafts into one best answer. Where drafts disagree, "
                                      "decide based on evidence and say why. Drop anything unsupported. Be complete but tight."},
        {"role": "user", "content": f"Question:\n{question}\n\n{body}\n\nWrite the single best final answer."},
    ], temperature=0.2, max_tokens=4096, num_ctx=rt.ctx_for(synth_p))
    return synth.content.strip() or body


@tool("contemplate", "For hard questions or high-stakes decisions: several subagents solve it independently from "
      "different angles, then their answers are synthesized. Slower, more reliable.",
      {"question": {"type": "string"}, "n": {"type": "integer", "description": "solvers, 2-5 (default 3)"}},
      ["question"], group="subagents")
def contemplate(rt, question: str, n: int = 3) -> str:
    return contemplate_run(rt, question, n)
