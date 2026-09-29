"""
Task Completion + Plan Quality eval for the LangGraph File Manager agent.

Both metrics grade the SAME run: the agent runs once per golden, deepeval's
`CallbackHandler` records one trace, and each metric reads that trace.

    Task Completion  — did the task get done?   (judges the outcome)
    Plan Quality     — was the plan any good?    (judges the plan the planner
                                                  wrote, not how it was carried out)

Running them together shows WHERE a failure comes from:
    good plan  + task done     → working as intended
    bad plan   + task done     → the executor covered for the planner (lucky)
    good plan  + task failed   → execution problem
    bad plan   + task failed   → planning problem

Goldens live in goldens.json: 15 tasks written against the workspace in
sandbox.py, each tagged easy / medium / difficult in additional_metadata.

Run:
    pip install -U deepeval langgraph langchain langchain-openai python-dotenv
    # put OPENAI_API_KEY=sk-... in a .env file in this folder
    python eval_agent.py

After the run, open reports/agent_eval_<date>_<time>.md for both scores per
task, and traces/<task>.judge.json for exactly what the judge read.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load OPENAI_API_KEY from .env before anything reads it.
load_dotenv()

from deepeval.dataset import EvaluationDataset
from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
from deepeval.integrations.langchain import CallbackHandler
from deepeval.metrics import PlanQualityMetric, TaskCompletionMetric

from agent import build_agent, run_agent
from report import write_report
from sandbox import reset_sandbox
from slim_trace import with_slim_trace

JUDGE_MODEL = "gpt-4o-mini"
THRESHOLD = 0.7


# ---------------------------------------------------------------------------
# 1. Metrics — both judge the slimmed trace (see slim_trace.py) and save what
#    the judge read to traces/. The plan the agent wrote is in each trace's
#    `planner` span.
# ---------------------------------------------------------------------------
SlimTaskCompletion = with_slim_trace(TaskCompletionMetric, save_dir="traces")
SlimPlanQuality = with_slim_trace(PlanQualityMetric, save_dir="traces")


# ---------------------------------------------------------------------------
# 2. Goldens
# ---------------------------------------------------------------------------
dataset = EvaluationDataset()
dataset.add_goldens_from_json_file(str(Path(__file__).parent / "goldens.json"))


# ---------------------------------------------------------------------------
# 3. Run the eval
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent = build_agent()

    # One golden at a time, with a pause, to stay under OpenAI rate limits.
    async_config = AsyncConfig(max_concurrent=1, throttle_value=15)

    rows = []   # (golden, {name: metric}) — scores filled in by deepeval after the loop
    try:
        for golden in dataset.evals_iterator(async_config=async_config):
            reset_sandbox()   # every golden starts from the same 20 files
            # A fresh pair for every golden — a metric stores its own score and reason.
            metrics = {
                "Task Completion": SlimTaskCompletion(
                    threshold=THRESHOLD, model=JUDGE_MODEL, include_reason=True, verbose_mode=True
                ),
                "Plan Quality": SlimPlanQuality(
                    threshold=THRESHOLD, model=JUDGE_MODEL, include_reason=True, verbose_mode=True
                ),
            }
            rows.append((golden, metrics))
            run_agent(
                agent,
                golden.input,
                callbacks=[CallbackHandler(metrics=list(metrics.values()))],
            )
    finally:
        # Always write the report, even if the run crashed or was stopped.
        if rows:
            write_report(rows, "agent_eval")