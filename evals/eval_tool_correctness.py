"""
Strict Tool Correctness eval: exactly the right tool calls, in the right order,
with the right arguments.

Uses goldens/goldens_strict.json: 10 tasks that each have ONE correct sequence
of tool calls. Score is 1 if the agent's calls match exactly, else 0.

How it works (deepeval's tracing route):
  - CallbackHandler records every tool call the agent makes on the trace
    (tools_called).
  - We put the golden's answer key on the trace ourselves with
    update_current_trace(expected_tools=...), because deepeval doesn't copy it
    from the golden.
  - evals_iterator(metrics=[...]) then scores each trace.

Run (from the project root):
    uv run python -m evals.eval_tool_correctness
"""

from dotenv import load_dotenv

load_dotenv()   # OPENAI_API_KEY from .env

from deepeval.dataset import EvaluationDataset
from deepeval.integrations.langchain import CallbackHandler
from deepeval.metrics import ToolCorrectnessMetric
from deepeval.test_case import ToolCallParams
from deepeval.tracing import observe, update_current_trace

from file_manager_agent.agent import build_agent, run_agent
from file_manager_agent.sandbox import PROJECT_ROOT, reset_sandbox

GOLDENS_PATH = PROJECT_ROOT / "goldens" / "goldens_strict.json"

agent = build_agent()

# Strict: same tools, same order, same arguments.
metric = ToolCorrectnessMetric(
    should_exact_match=True,
    evaluation_params=[ToolCallParams.INPUT_PARAMETERS],
)


# open a blank sheet for this task
@observe()                     
def run(golden):
    # write the question and the answer key on the sheet
    update_current_trace(input=golden.input, expected_tools=golden.expected_tools)

    # run the agent; the CallbackHandler writes each tool call it makes onto the same sheet
    state = run_agent(agent, golden.input, callbacks=[CallbackHandler()], full=True)

    # write the agent's final reply at the bottom (not graded, just kept)
    update_current_trace(output=state["response"])
    # function ends → sheet is closed and handed to the metric


if __name__ == "__main__":
    dataset = EvaluationDataset()
    dataset.add_goldens_from_json_file(str(GOLDENS_PATH))

    for golden in dataset.evals_iterator(metrics=[metric]):
        reset_sandbox()   # every task starts from the same workspace
        run(golden)