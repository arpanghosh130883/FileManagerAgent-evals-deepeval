"""
File Manager agent built with LangGraph + LangChain — PLAN-AND-EXECUTE style.

Graph:

    START ──► planner ──► executor ──(steps left?)──► executor ...
                              │
                              └──(all steps done)──► responder ──► END

- planner   : one LLM call that writes an explicit, numbered plan. It is shown
              a listing of the workspace first, so it plans with real file
              names instead of guessing them
              (structured output -> list of steps). This makes the agent's
              planning visible in the trace, which the Plan Quality and
              Plan Adherence metrics need.
- executor  : carries out ONE plan step per visit, using a small tool-calling
              sub-agent (LangChain `create_agent`). It sees the whole plan and
              what's been done so far, but is told to do only the current step.
- responder : writes the final answer from the step results.

Everything happens inside the `workspace/` folder, which is defined and reset
in sandbox.py. The tools below can only touch files inside it.

Setup — create a file named .env in this folder containing:
    OPENAI_API_KEY=sk-...

Try it directly:
    python agent.py "Move budget.xlsx into the archive folder"
"""

import json
import operator
import shutil
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from sandbox import SANDBOX, list_workspace, safe_path

# Read OPENAI_API_KEY (and any other settings) from a .env file next to this script.
load_dotenv()


# ===========================================================================
# 1. Tools — the @tool docstring is what the model reads to pick a tool
# ===========================================================================
@tool
def list_files(folder: str = ".") -> str:
    """List files and folders inside a workspace folder. Use '.' for the root."""
    p = safe_path(folder)
    if not p.is_dir():
        return f"Error: folder '{folder}' not found"
    return json.dumps([f"{c.name}/" if c.is_dir() else c.name for c in sorted(p.iterdir())])


@tool
def read_file(path: str) -> str:
    """Read the text content of a file."""
    p = safe_path(path)
    return p.read_text() if p.is_file() else f"Error: file '{path}' not found"


@tool
def create_file(path: str, content: str) -> str:
    """Create (or overwrite) a file with the given text content."""
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Created {path}"


@tool
def create_folder(path: str) -> str:
    """Create a folder (and any missing parent folders)."""
    safe_path(path).mkdir(parents=True, exist_ok=True)
    return f"Created folder {path}/"


@tool
def move_file(source: str, destination: str) -> str:
    """Move a file to a destination folder or path. The source is removed."""
    src, dst = safe_path(source), safe_path(destination)
    if not src.exists():
        return f"Error: '{source}' not found"
    if dst.is_dir():
        dst = dst / src.name
    shutil.move(str(src), str(dst))
    return f"Moved {source} -> {dst.relative_to(SANDBOX)}"


@tool
def delete_file(path: str) -> str:
    """Permanently delete a single file."""
    p = safe_path(path)
    if not p.is_file():
        return f"Error: file '{path}' not found"
    p.unlink()
    return f"Deleted {path}"


TOOLS = [list_files, read_file, create_file, create_folder, move_file, delete_file]
TOOL_LIST = "\n".join(f"- {t.name}: {t.description}" for t in TOOLS)


# ===========================================================================
# 2. Prompts
# ===========================================================================
PLANNER_PROMPT = (
    "You are the PLANNER of a file-manager agent that works in a sandboxed workspace.\n"
    "Break the user's task into the smallest set of concrete steps needed to complete it.\n"
    "Each step must be one action that names the tool and its arguments, e.g.\n"
    "  \"Call move_file with source='budget.xlsx' and destination='archive'\".\n"
    "Use at most 10 steps and no unnecessary steps. Paths are relative to the workspace "
    "root.\n"
    "You are given a listing of every file and folder currently in the workspace "
    "(folders end with '/'). Use only file and folder names that appear in that listing, "
    "or that the task asks you to create. Never invent a name. The listing shows names "
    "only: if the task depends on what files contain, add steps to read them.\n\n"
    f"Available tools:\n{TOOL_LIST}"
)

EXECUTOR_PROMPT = (
    "You are the EXECUTOR of a file-manager agent working in a sandboxed workspace. "
    "You will be given the overall task, the full plan, the results of steps already "
    "done, and the CURRENT step. Carry out only the current step using the tools, then "
    "reply with a short factual result of that step (include any information found). "
    "Paths are relative to the workspace root."
)

RESPONDER_PROMPT = (
    "You are the RESPONDER of a file-manager agent. Given the user's task and the results "
    "of each executed step, reply to the user in one or two sentences with what was done "
    "or the answer they asked for. Only report what the step results show."
)


# ===========================================================================
# 3. Graph state
# ===========================================================================
class Plan(BaseModel):
    """The planner's structured output."""
    steps: list[str] = Field(description="Ordered, concrete steps to complete the task")


class PlanExecuteState(TypedDict):
    task: str                                                   # the user's request
    plan: list[str]                                             # written by the planner
    past_steps: Annotated[list[tuple[str, str]], operator.add]  # (step, result), appended
    response: str                                               # final answer


# ===========================================================================
# 4. The graph
# ===========================================================================
def build_agent(llm=None):
    """Build and compile the plan-and-execute agent. Pass `llm` to swap the model."""
    llm = llm or ChatOpenAI(model="gpt-4o-mini", temperature=0)
    planner_llm = llm.with_structured_output(Plan)
    step_executor = create_agent(llm, tools=TOOLS, system_prompt=EXECUTOR_PROMPT,
                                 name="step_executor")

    # --- planner: writes the plan once --------------------------------------
    def planner(state: PlanExecuteState):
        # Look at the workspace NOW, so the plan uses real names instead of guesses.
        listing = "\n".join(list_workspace())
        plan = planner_llm.invoke([
            SystemMessage(PLANNER_PROMPT),
            HumanMessage(f"TASK: {state['task']}\n\nCURRENT WORKSPACE:\n{listing}"),
        ])
        return {"plan": plan.steps}

    # --- executor: one plan step per visit ----------------------------------
    def executor(state: PlanExecuteState):
        plan, done = state["plan"], state["past_steps"]
        i = len(done)
        step = plan[i]
        plan_text = "\n".join(f"{n}. {s}" for n, s in enumerate(plan, 1))
        done_text = "\n".join(f"{n}. {s} -> {r}" for n, (s, r) in enumerate(done, 1)) or "(none yet)"
        prompt = (
            f"TASK: {state['task']}\n\nPLAN:\n{plan_text}\n\n"
            f"COMPLETED STEPS:\n{done_text}\n\n"
            f"CURRENT STEP ({i + 1}): {step}"
        )
        result = step_executor.invoke({"messages": [HumanMessage(prompt)]})
        return {"past_steps": [(step, result["messages"][-1].content)]}

    def more_steps(state: PlanExecuteState) -> str:
        return "executor" if len(state["past_steps"]) < len(state["plan"]) else "responder"

    # --- responder: final answer --------------------------------------------
    def responder(state: PlanExecuteState):
        results = "\n".join(f"- {s}: {r}" for s, r in state["past_steps"])
        msg = llm.invoke([
            SystemMessage(RESPONDER_PROMPT),
            HumanMessage(f"TASK: {state['task']}\n\nSTEP RESULTS:\n{results}"),
        ])
        return {"response": msg.content}

    graph = StateGraph(PlanExecuteState)
    graph.add_node("planner", planner)
    graph.add_node("executor", executor)
    graph.add_node("responder", responder)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_conditional_edges("executor", more_steps, ["executor", "responder"])
    graph.add_edge("responder", END)
    return graph.compile(name="file_manager_agent")


def run_agent(agent, task: str, callbacks=None) -> str:
    """Run one task and return the agent's final answer."""
    result = agent.invoke(
        {"task": task, "past_steps": []},
        config={"callbacks": callbacks or [], "recursion_limit": 40},
    )
    return result["response"]