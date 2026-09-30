# Agent Evals with DeepEval

Evaluating a **LangGraph file-manager agent** with [DeepEval](https://github.com/confident-ai/deepeval).

The agent takes plain-English requests ("Move budget.xlsx into the archive folder") and carries them out inside a sandboxed `data/workspace/` folder. Each run is scored three ways:

- **Task Completion** (DeepEval): did the task actually get done? This judges the outcome from the run's trace.
- **Plan Quality** (DeepEval): was the agent's plan any good? This judges the plan from the trace, but never sees the workspace or the tools.
- **Plan Quality (sees workspace)** (our own G-Eval judge in `evals/plan_judge.py`): grades the exact plan against the workspace listing the planner saw and the tool list.

Scoring the same run on the outcome and on the plan shows **where** a failure comes from:

| | Task done | Task failed |
|---|---|---|
| **Good plan** | Working as intended | Execution problem |
| **Bad plan** | Executor covered for the planner | Planning problem |

## The agent

`src/file_manager_agent/agent.py` is a **plan-and-execute** agent built with LangGraph + LangChain:

```
START ──► planner ──► executor ──(steps left?)──► executor ...
                          │
                          └──(all steps done)──► responder ──► END
```

- **planner** (`gpt-5.6-terra`): sees the current workspace listing and writes a numbered plan (structured output). The plan is its own step in the trace, which is what Plan Quality reads.
- **executor** (`gpt-4o-mini`): carries out one plan step per visit, using a small tool-calling sub-agent.
- **responder** (`gpt-4o-mini`): writes the final answer from the step results.

Tools: `list_files`, `read_file`, `create_file`, `create_folder`, `move_file`, `delete_file`, `delete_folder`. Every path goes through `safe_path()` in `sandbox.py`, so the agent can't touch anything outside `data/workspace/`.

## Project layout

```
.
├── main.py                     # run the agent on one task from the command line
├── src/file_manager_agent/     # the agent (installed as a package by `uv sync`)
│   ├── agent.py                #   graph, prompts, tools, run_agent()
│   └── sandbox.py              #   the 20 starting files, reset_sandbox(), safe_path()
├── evals/                      # all evaluation code, run as modules: python -m evals.<name>
│   ├── eval_agent.py           #   main eval: Task Completion + both Plan Quality scores
│   ├── eval_task_completion.py #   simpler eval: Task Completion only
│   ├── plan_judge.py           #   our plan judge that sees the workspace and tools
│   ├── check_plan_judge.py     #   sanity check: does the plan judge tell bad plans from good?
│   ├── slim_trace.py           #   shrinks the LangGraph trace before a judge reads it
│   ├── report.py               #   writes a Markdown + CSV report of each eval run
│   └── results/                #   eval outputs (git-ignored)
│       ├── reports/            #     <name>_<date>_<time>.md / .csv
│       └── traces/             #     <task>.raw.json and <task>.judge.json
├── goldens/goldens.json        # 15 test tasks, tagged easy / medium / difficult
└── data/                       # runtime data (git-ignored, rebuilt by reset_sandbox())
    ├── workspace/              #   the agent's sandbox, wiped before every run
    └── workspace_original/     #   read-only copy of the starting state, for comparison
```

## Setup

Uses [uv](https://docs.astral.sh/uv/) and Python 3.11:

```bash
uv sync
```

The dependency versions are pinned in `pyproject.toml` to the ones the evals were run with, because a newer DeepEval or LangChain can change the judge prompts or the trace shape, and with them the scores.

Create a `.env` file in the project root:

```
OPENAI_API_KEY=sk-...
```

## Running

Run every command from the project root.

Run the agent on one task. The sandbox is reset first, then the plan and the answer are printed:

```bash
uv run main.py "Rename todo.txt to tasks.txt"
```

Reset the workspace and print its layout (no API calls):

```bash
uv run python -m file_manager_agent.sandbox
```

Run the full eval (Task Completion + DeepEval Plan Quality + our plan judge):

```bash
uv run python -m evals.eval_agent
```

Or only Task Completion:

```bash
uv run python -m evals.eval_task_completion
```

Sanity-check the plan judge on plans we already know are bad or good. No agent runs here, only the judge:

```bash
uv run python -m evals.check_plan_judge
```

Goldens run one at a time with a pause between them (`max_concurrent=1, throttle_value=15`) to stay under OpenAI rate limits. Afterwards, open:

- `evals/results/reports/agent_eval_<date>_<time>.md`: scores per task plus each judge's reasoning (failures first). There is a `.csv` next to it for spreadsheets.
- `evals/results/traces/<task>.judge.json`: exactly what the trace judges were shown for that task (`<task>.raw.json` is the full trace before slimming).

## The goldens

The 15 tasks in `goldens/goldens.json` are written against the workspace defined in `sandbox.py`:

- **easy**: one action, and the exact file is named. *"Delete cache.tmp from the workspace root"*
- **medium**: the agent has to look first, or use a tool creatively. *"Rename todo.txt to tasks.txt"* (there's no rename tool, so it has to use `move_file`)
- **difficult**: many steps, reasoning over file contents, or requests that are partly impossible with the available tools. *"Move q2_2024.pdf and q2_2025.pdf from reports into the archive folder"* (one of them doesn't exist). For these, the correct outcome includes saying honestly what couldn't be done.

If you change `WORKSPACE_FILES` in `sandbox.py`, check that the goldens still make sense.

## Why our own plan judge?

DeepEval's `PlanQualityMetric` grades a plan from the task and a paraphrase of the plan only. It never sees the workspace or the tools, so it can't tell a complete plan from an incomplete one: "delete these 3 .tmp files" looks incomplete unless you know there are exactly 3.

`evals/plan_judge.py` is a G-Eval metric (judge: `gpt-4o`) that gets the task, the plan word for word, the workspace listing, the tool list, and a code-computed list of files the plan never mentions. `evals/check_plan_judge.py` checks that it FAILs known-bad plans and PASSes known-good ones before you trust its scores.

## Why `slim_trace.py`?

DeepEval's LangChain `CallbackHandler` saves each LangGraph step with its full input and output. In a plan-and-execute graph that repeats a lot: the whole graph state goes into every node, and the whole message history goes into every LLM call. One run can come to about 50k tokens, which can be more than an OpenAI rate limit allows in one request.

`with_slim_trace(MetricClass, save_dir=...)` wraps any DeepEval metric that reads traces (Task Completion, Plan Quality, Plan Adherence, Step Efficiency, ...). The judge still sees every fact once: the plan, each step's result, tool arguments and outputs, and the final answer. Repeated inputs and outputs are dropped, and routing-only steps are removed.

## Sample results

From the 30 Sep 2026 10:58 run (trace judges: `gpt-4o-mini`, plan judge: `gpt-4o`, threshold 0.7):

| Metric | Average score | Passed |
|---|---|---|
| Task Completion | 0.93 | 13/15 |
| Plan Quality (DeepEval) | 0.58 | 8/15 |
| Plan Quality (sees workspace) | 0.91 | 15/15 |

The agent usually gets the job done. DeepEval's Plan Quality scores the plans low, but the judge that can see the workspace rates them highly: most of the gap comes from the context DeepEval's metric is missing, not from bad plans.
