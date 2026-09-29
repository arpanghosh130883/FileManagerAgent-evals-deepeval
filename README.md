# Agent Evals with DeepEval

Evaluating a **LangGraph file-manager agent** with [DeepEval](https://github.com/confident-ai/deepeval).

The agent takes plain-English requests ("Move budget.xlsx into the archive folder") and carries them out inside a sandboxed `workspace/` folder. DeepEval records every run as a trace and uses an LLM judge to score it on two metrics:

- **Task Completion**: did the task actually get done? (judges the outcome)
- **Plan Quality**: was the agent's plan any good? (judges the plan, not how it was carried out)

Scoring the same run on both metrics shows **where** a failure comes from:

| | Task done | Task failed |
|---|---|---|
| **Good plan** | Working as intended | Execution problem |
| **Bad plan** | Executor covered for the planner | Planning problem |

## The agent

`agent.py` is a **plan-and-execute** agent built with LangGraph + LangChain (`gpt-4o-mini`):

```
START ──► planner ──► executor ──(steps left?)──► executor ...
                          │
                          └──(all steps done)──► responder ──► END
```

- **planner**: looks at the current workspace listing and writes a numbered plan (structured output). The plan is a separate step in the trace, which is what Plan Quality reads.
- **executor**: carries out one plan step per visit, using a small tool-calling sub-agent.
- **responder**: writes the final answer from the step results.

Tools: `list_files`, `read_file`, `create_file`, `create_folder`, `move_file`, `delete_file`. Every path goes through `safe_path()` in `sandbox.py`, so the agent can't touch anything outside `workspace/`.

## Project layout

| File / folder | What it is |
|---|---|
| `agent.py` | The LangGraph agent and its tools |
| `sandbox.py` | Defines the starting workspace (20 files) and resets it before each golden |
| `goldens.json` | 15 test tasks, tagged `easy` / `medium` / `difficult` |
| `eval_agent.py` | Main eval: Task Completion + Plan Quality on each run |
| `eval_task_completion.py` | Simpler eval: Task Completion only |
| `slim_trace.py` | Shrinks the LangGraph trace before the judge reads it (see below) |
| `report.py` | Writes a Markdown + CSV report of each eval run |
| `reports/` | Reports from past runs |
| `traces/` | For each task, the raw trace (`*.raw.json`) and the slimmed version the judge saw (`*.judge.json`) |
| `workspace/` | The agent's sandbox. Wiped and rebuilt before every golden |
| `workspace_original/` | Read-only copy of the starting state, for comparison |

## Setup

```bash
pip install -U deepeval langgraph langchain langchain-openai python-dotenv
```

Create a `.env` file in the project folder:

```
OPENAI_API_KEY=sk-...
```

## Running

Try the agent on a single task:

```bash
python agent.py "Move budget.xlsx into the archive folder"
```

Reset the workspace and print its layout:

```bash
python sandbox.py
```

Run the full eval (Task Completion + Plan Quality):

```bash
python eval_agent.py
```

Or only Task Completion:

```bash
python eval_task_completion.py
```

Goldens run one at a time with a pause between them (`max_concurrent=1, throttle_value=15`) to stay under OpenAI rate limits. Afterwards, open:

- `reports/agent_eval_<date>_<time>.md`: scores per task plus the judge's reasoning (failures first). There is a `.csv` next to it for spreadsheets.
- `traces/<task>.judge.json`: exactly what the judge was shown for that task.

## The goldens

The 15 tasks in `goldens.json` are written against the workspace defined in `sandbox.py`:

- **easy**: one action, and the exact file is named. *"Delete cache.tmp from the workspace root"*
- **medium**: the agent has to look first, or use a tool creatively. *"Rename todo.txt to tasks.txt"* (there's no rename tool, so it has to use `move_file`)
- **difficult**: many steps, reasoning over file contents, or requests that are partly impossible with the available tools. *"Delete the logs folder completely"* (`delete_file` only removes files). For these, the correct outcome includes saying honestly what couldn't be done.

If you change `WORKSPACE_FILES` in `sandbox.py`, check that the goldens still make sense.

## Why `slim_trace.py`?

DeepEval's LangChain `CallbackHandler` saves each LangGraph step with its full input and output. In a plan-and-execute graph that repeats a lot: the whole graph state goes into every node, and the whole message history goes into every LLM call. One run can come to about 50k tokens, which can be more than an OpenAI rate limit allows in one request.

`with_slim_trace(MetricClass, save_dir="traces")` wraps any DeepEval metric that reads traces (Task Completion, Plan Quality, Plan Adherence, Step Efficiency, ...). The judge still sees every fact once: the plan, each step's result, tool arguments and outputs, and the final answer. Repeated inputs and outputs are dropped, and routing-only steps are removed.

## Sample results

From `reports/agent_eval_20260929_1042.md` (judge: `gpt-4o-mini`, threshold 0.7):

| Metric | Average score | Passed |
|---|---|---|
| Task Completion | 0.90 | 13/15 |
| Plan Quality | 0.53 | 6/15 |

The agent usually gets the job done, but its plans often score low. That puts many tasks in the "executor covered for the planner" box of the table at the top.
