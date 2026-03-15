"""
LangGraph-based agent for the California Backpacking Trail Advisor.

Phase 1 (Gather): gather_llm <-> tool_exec  (loop until trigger_search fires)
Phase 2 (Recommend): search_node -> weather_node -> permit_node -> photo_node -> recommend_node -> END
"""

import json
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from langchain_openai import ChatOpenAI

from tools import (
    search_trails_raw,
    get_weather,
    get_permit_info,
    get_trail_photo,
    set_preferences,
)

# ─── Configuration ────────────────────────────────────────────────────────────

OLLAMA_BASE = "http://100.86.195.79:11434/v1"
MODEL = "qwen3"

# ─── State ────────────────────────────────────────────────────────────────────

class TrailState(TypedDict):
    messages: Annotated[list, add_messages]
    preferences: dict
    top_trail: dict | None
    trail_result: str
    weather_result: str
    permit_result: str
    photo_result: str


# ─── Prompts ──────────────────────────────────────────────────────────────────

GATHER_SYSTEM = """\
You are Backcountry, a California backpacking trail advisor.

Your job right now is to learn what the user wants through friendly conversation.

Preferences to collect:
1. Where (region/park) — e.g. "Yosemite", "Sierra Nevada", "Big Sur"
2. Difficulty — easy / moderate / hard / very hard
3. Length (miles)
4. Features — views, lake, waterfall, forest, river, wildlife, wildflowers
5. Route type — loop / out-and-back / point-to-point

RULES:
- Call set_preferences every time you learn something new. Partial updates are fine.
- After recording preferences, ask a natural follow-up question for the next missing preference.
- When the user has ≥3 preferences AND explicitly says they are ready, call trigger_search.
- NEVER list or recommend trails during this phase.
- Be warm, conversational, and brief.
"""

RECOMMEND_SYSTEM = """\
You are Backcountry, a California backpacking trail advisor.

All research data is provided in the user's message. Write a warm, friendly recommendation.

RULES:
- Recommend EXACTLY ONE trail. No lists, no options.
- Use ONLY the provided data — never invent distances, elevation, or weather.
- If a PHOTO_URL line is present in the data, copy it VERBATIM as the very first line of your response.
- Include: trail name (exactly as given), why it fits the user's preferences, weather outlook, permit details.
"""

# ─── Gather-phase tool schemas ─────────────────────────────────────────────────

_GATHER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_preferences",
            "description": "Record user preferences as they're revealed. Call immediately when user mentions region, difficulty, length, features, or route type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region":     {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["easy", "moderate", "hard", "very hard"]},
                    "min_miles":  {"type": "number"},
                    "max_miles":  {"type": "number"},
                    "features":   {"type": "array", "items": {"type": "string"}},
                    "route_type": {"type": "string", "enum": ["loop", "out-and-back", "point-to-point"]},
                    "trip_days":  {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_search",
            "description": "Call this ONLY when the user has ≥3 preferences AND explicitly says they are ready to see a trail recommendation (e.g. 'find me a trail', 'let's go', 'show me options').",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# ─── LLM clients ──────────────────────────────────────────────────────────────

def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=OLLAMA_BASE,
        api_key="ollama",
        model=MODEL,
    )


_gather_llm = _make_llm().bind_tools(_GATHER_TOOLS)
_recommend_llm = _make_llm()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _to_lc_messages(history: list[dict]) -> list:
    """Convert plain role/content dicts to LangChain message objects."""
    out = []
    for m in history:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        # skip tool / system roles
    return out


def _prefs_to_kwargs(prefs: dict) -> dict:
    """Map accumulated preferences dict to search_trails_raw keyword args."""
    kwargs = {}
    if prefs.get("region"):
        kwargs["region"] = prefs["region"]
    if prefs.get("difficulty"):
        kwargs["difficulty"] = prefs["difficulty"]
    if prefs.get("min_miles"):
        kwargs["min_miles"] = prefs["min_miles"]
    if prefs.get("max_miles"):
        kwargs["max_miles"] = prefs["max_miles"]
    if prefs.get("features"):
        kwargs["features"] = prefs["features"]
    if prefs.get("route_type"):
        kwargs["route_type"] = prefs["route_type"]
    return kwargs


# ─── Graph nodes ──────────────────────────────────────────────────────────────

def gather_llm_node(state: TrailState) -> dict:
    """Call the gather LLM with set_preferences + trigger_search tools available."""
    system = SystemMessage(content=GATHER_SYSTEM)
    response = _gather_llm.invoke([system] + state["messages"])
    return {"messages": [response]}


def tool_exec_node(state: TrailState) -> dict:
    """Execute tool calls from the most recent AIMessage."""
    # Find the last AIMessage
    last_ai = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            last_ai = msg
            break

    if last_ai is None or not getattr(last_ai, "tool_calls", None):
        return {}

    prefs = dict(state.get("preferences") or {})
    tool_messages = []

    for tc in last_ai.tool_calls:
        fn_name = tc["name"]
        fn_args = tc["args"] if isinstance(tc["args"], dict) else json.loads(tc["args"])
        tool_call_id = tc["id"]

        if fn_name == "set_preferences":
            result = set_preferences(**fn_args)
            # Parse PREFS_UPDATED sentinel and accumulate
            if result.startswith("PREFS_UPDATED:"):
                try:
                    partial = json.loads(result[len("PREFS_UPDATED:"):].strip())
                    prefs.update(partial)
                except Exception:
                    pass
        elif fn_name == "trigger_search":
            result = "Search triggered."
        else:
            result = f"Unknown tool: {fn_name}"

        from langchain_core.messages import ToolMessage
        tool_messages.append(
            ToolMessage(content=result, tool_call_id=tool_call_id)
        )

    return {"messages": tool_messages, "preferences": prefs}


def search_node(state: TrailState) -> dict:
    """Run search_trails_raw with accumulated preferences; store top trail."""
    prefs = state.get("preferences") or {}
    kwargs = _prefs_to_kwargs(prefs)

    results = search_trails_raw(**kwargs)

    # Fallback: retry without difficulty if no results
    if not results and kwargs.get("difficulty"):
        fallback_kwargs = {k: v for k, v in kwargs.items() if k != "difficulty"}
        results = search_trails_raw(**fallback_kwargs)

    if not results:
        trail_result = "No trails found matching those criteria."
        return {"top_trail": None, "trail_result": trail_result}

    top = results[0]
    lines = [f"Top trail match:\n"]
    lines.append(
        f"• {top['name']} ({top['area']})\n"
        f"  {top['length_miles']} mi | +{top['elev_gain_ft']} ft | {top['difficulty']} | "
        f"{top['route_type']} | ★ {top['avg_rating']} ({top['num_reviews']} reviews)\n"
        f"  Features: {', '.join(top['features'][:6]) or 'none listed'}\n"
        f"  Coords: {top['lat']}, {top['lng']}\n"
    )
    trail_result = "\n".join(lines)

    return {"top_trail": top, "trail_result": trail_result}


def weather_node(state: TrailState) -> dict:
    """Fetch NWS weather for the top trail's coordinates."""
    trail = state.get("top_trail")
    if not trail:
        return {"weather_result": "No trail selected — weather unavailable."}
    result = get_weather(lat=trail["lat"], lng=trail["lng"])
    return {"weather_result": result}


def permit_node(state: TrailState) -> dict:
    """Fetch permit info for the top trail's area."""
    trail = state.get("top_trail")
    if not trail:
        return {"permit_result": "No trail selected — permit info unavailable."}
    result = get_permit_info(area_name=trail["area"])
    return {"permit_result": result}


def photo_node(state: TrailState) -> dict:
    """Fetch a photo for the top trail."""
    trail = state.get("top_trail")
    if not trail:
        return {"photo_result": ""}
    result = get_trail_photo(
        lat=trail["lat"],
        lng=trail["lng"],
        trail_name=trail["name"],
        area_name=trail.get("area", ""),
    )
    return {"photo_result": result}


def recommend_node(state: TrailState) -> dict:
    """Generate the final recommendation using all phase 2 data."""
    trail_result = state.get("trail_result", "")
    weather_result = state.get("weather_result", "")
    permit_result = state.get("permit_result", "")
    photo_result = state.get("photo_result", "")
    prefs = state.get("preferences") or {}

    context_parts = []
    if photo_result:
        context_parts.append(photo_result)
    context_parts.append(f"TRAIL:\n{trail_result}")
    context_parts.append(f"WEATHER:\n{weather_result}")
    context_parts.append(f"PERMITS:\n{permit_result}")
    context_parts.append(f"USER PREFERENCES:\n{json.dumps(prefs, indent=2)}")

    context_message = "\n\n".join(context_parts)

    system = SystemMessage(content=RECOMMEND_SYSTEM)
    human = HumanMessage(content=context_message)
    response = _recommend_llm.invoke([system, human])

    return {"messages": [response]}


# ─── Routing ──────────────────────────────────────────────────────────────────

def route_after_gather(state: TrailState) -> str:
    """After gather_llm: go to tool_exec if there are tool calls, else END."""
    # Find the most recent AIMessage
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:  # non-empty list
                return "tool_exec"
            return END
    return END


def route_after_tools(state: TrailState) -> str:
    """After tool_exec: if trigger_search was called, go to search_node; else loop back."""
    # Find the most recent AIMessage to check if trigger_search was called
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    name = tc["name"] if isinstance(tc, dict) else tc.name
                    if name == "trigger_search":
                        return "search_node"
            return "gather_llm"
    return "gather_llm"


# ─── Graph construction ───────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    builder = StateGraph(TrailState)

    builder.add_node("gather_llm", gather_llm_node)
    builder.add_node("tool_exec", tool_exec_node)
    builder.add_node("search_node", search_node)
    builder.add_node("weather_node", weather_node)
    builder.add_node("permit_node", permit_node)
    builder.add_node("photo_node", photo_node)
    builder.add_node("recommend_node", recommend_node)

    builder.set_entry_point("gather_llm")

    builder.add_conditional_edges(
        "gather_llm",
        route_after_gather,
        {"tool_exec": "tool_exec", END: END},
    )
    builder.add_conditional_edges(
        "tool_exec",
        route_after_tools,
        {"search_node": "search_node", "gather_llm": "gather_llm"},
    )

    # Deterministic recommend pipeline
    builder.add_edge("search_node", "weather_node")
    builder.add_edge("weather_node", "permit_node")
    builder.add_edge("permit_node", "photo_node")
    builder.add_edge("photo_node", "recommend_node")
    builder.add_edge("recommend_node", END)

    return builder.compile()


# Build the graph once at module load
_graph = _build_graph()


# ─── Public API ───────────────────────────────────────────────────────────────

def run(
    user_message: str,
    history: list[dict],
    accumulated_prefs: dict | None = None,
) -> tuple[str, list[dict], dict]:
    """
    Run one turn of the agent.

    Returns (response_text, updated_history, accumulated_preferences).
    accumulated_prefs carries preference state across turns.
    """
    prefs = dict(accumulated_prefs or {})

    lc_messages = _to_lc_messages(history)
    lc_messages.append(HumanMessage(content=user_message))

    initial_state: TrailState = {
        "messages": lc_messages,
        "preferences": prefs,
        "top_trail": None,
        "trail_result": "",
        "weather_result": "",
        "permit_result": "",
        "photo_result": "",
    }

    result = _graph.invoke(initial_state)

    # Find last AIMessage with non-empty content
    response_text = ""
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            response_text = msg.content
            break

    if not response_text:
        response_text = "I wasn't able to produce a response. Please try again."

    new_history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": response_text},
    ]

    return response_text, new_history, result["preferences"]
