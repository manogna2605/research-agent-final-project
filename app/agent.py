"""
The oracle + LangGraph wiring.
Builds the graph fresh per run using the given user's API keys.
"""
import operator
from typing import Annotated, List, TypedDict

from langchain_core.agents import AgentAction
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from .tools import make_tools

SYSTEM_PROMPT = """You are the oracle, the great AI decision-maker.
Given the user's query, you must decide what to do with it based on the
list of tools provided to you.

If you see that a tool has been used (in the scratchpad) with a particular
query, do NOT use that same tool with the same query again. Also, do NOT use
any tool more than twice.

You should aim to collect information from a diverse range of sources before
providing the answer to the user. Once you have collected plenty of
information to answer the user's question, use the final_answer tool."""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    MessagesPlaceholder(variable_name="chat_history"),
    ("user", "{input}"),
    ("assistant", "scratchpad: {scratchpad}"),
])


class AgentState(TypedDict):
    input: str
    chat_history: List[BaseMessage]
    intermediate_steps: Annotated[List[AgentAction], operator.add]


def _scratchpad(steps: list) -> str:
    return "\n---\n".join(
        f"Tool: {a.tool}, input: {a.tool_input}\nOutput: {a.log}"
        for a in steps if a.log != "TBD"
    )


def _build_graph(user_keys: dict):
    tools = make_tools(user_keys)
    tool_map = {t.name: t for t in tools}
    openai_key = user_keys.get("openai_key", "placeholder")

    llm = ChatOpenAI(model="gpt-4o", api_key=openai_key, temperature=0)
    oracle_chain = (
        {
            "input":              lambda x: x["input"],
            "chat_history":       lambda x: x["chat_history"],
            "scratchpad":         lambda x: _scratchpad(x["intermediate_steps"]),
        }
        | PROMPT
        | llm.bind_tools(tools, tool_choice="any")
    )

    def run_oracle(state):
        out = oracle_chain.invoke(state)
        tc = out.tool_calls[0]
        return {"intermediate_steps": [AgentAction(tool=tc["name"], tool_input=tc["args"], log="TBD")]}

    def router(state):
        steps = state.get("intermediate_steps", [])
        return steps[-1].tool if steps else "final_answer"

    def run_tool(state):
        action = state["intermediate_steps"][-1]
        output = tool_map[action.tool].invoke(input=action.tool_input)
        return {"intermediate_steps": [AgentAction(tool=action.tool, tool_input=action.tool_input, log=str(output))]}

    graph = StateGraph(AgentState)
    graph.add_node("oracle", run_oracle)
    for t in tools:
        graph.add_node(t.name, run_tool)
    graph.set_entry_point("oracle")
    graph.add_conditional_edges(source="oracle", path=router)
    for t in tools:
        if t.name != "final_answer":
            graph.add_edge(t.name, "oracle")
    graph.add_edge("final_answer", END)
    return graph.compile()


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    return [line.strip("- ").strip() for line in str(value).split("\n") if line.strip()]


def run_agent_stream(query: str, user_keys: dict, chat_history: list = None):
    """Yields SSE-ready event dicts, using the given user's API keys."""
    chat_history = chat_history or []
    inputs = {"input": query, "chat_history": chat_history, "intermediate_steps": []}
    runnable = _build_graph(user_keys)

    for step in runnable.stream(inputs, config={"recursion_limit": 25}):
        for node_name, node_output in step.items():
            steps = node_output.get("intermediate_steps", [])
            if not steps:
                continue
            action = steps[-1]
            if node_name == "oracle":
                yield {"type": "decision", "tool": action.tool, "input": action.tool_input}
            elif node_name == "final_answer":
                yield {"type": "final", "report": {
                    "introduction":   action.tool_input.get("introduction", ""),
                    "research_steps": _as_list(action.tool_input.get("research_steps", "")),
                    "main_body":      action.tool_input.get("main_body", ""),
                    "conclusion":     action.tool_input.get("conclusion", ""),
                    "sources":        _as_list(action.tool_input.get("sources", "")),
                }}
            else:
                yield {"type": "tool_result", "tool": node_name, "input": action.tool_input, "output": action.log}
