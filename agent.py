"""
Agent loop using Ollama via the OpenAI-compatible endpoint.
Calls tools until the model produces a final answer.
"""

import json
import re
from openai import OpenAI
from tools import TOOLS, TOOL_MAP

# Patterns that indicate the model narrated a tool call as prose instead of
# invoking it.  When these appear with no actual tool_calls, we retry.
_NARRATED_TOOL_CALL_PATTERNS = re.compile(
    r"\[Calling\b"           # [Calling search_trails now]
    r"|\bI(?:'ll| will| am going to| would) (?:use|call|invoke|run)\b"
    r"|\bLet me (?:call|use|invoke|run)\b"
    r"|\bUsing the \w+ tool\b"
    r"|\bcalling \w+\(",
    re.IGNORECASE,
)

OLLAMA_BASE = "http://100.86.195.79:11434/v1"
MODEL = "qwen3"  # swap to any model you have: `ollama list`

SYSTEM_PROMPT = """\
You are Backcountry, a California backpacking trail advisor. You help users find the perfect trail through friendly conversation.

## PHASE 1 — GATHER PREFERENCES (default phase)

Your first job is to learn what the user wants. Each time the user mentions a preference, call set_preferences immediately to record it.

Preferences to collect:
1. Where (region/park) — e.g. "Yosemite", "Sierra Nevada", "Big Sur"
2. Difficulty — easy / moderate / hard / very hard
3. Length (miles) — how long a trip they want
4. Features — views, lake, waterfall, forest, river, wildlife, wildflowers
5. Route type — loop / out-and-back / point-to-point

PHASE 1 RULES:
- Call set_preferences every time you learn something new. Partial updates are fine.
- After calling set_preferences, ask a natural follow-up question for the next missing preference.
- Do NOT call search_trails until the user explicitly says they are ready (e.g. "find me a trail", "let's go", "show me options") OR until you have at least 3 preferences collected AND the user seems ready.
- Never list trails or make recommendations in Phase 1.
- Be warm, conversational, and brief.

## PHASE 2 — RECOMMEND A TRAIL

Triggered when user says they're ready, or you have ≥3 preferences and context suggests they want results.

PHASE 2 RULES (execute in this exact order):
1. Call search_trails with all collected preferences.
2. From the results, pick the BEST single trail. Call get_weather with its lat/lng.
3. Call get_permit_info with the trail's area name.
4. Call get_trail_photo with the trail's lat, lng, trail_name, and area_name. Include the returned PHOTO_URL line VERBATIM at the very start of your response — copy it exactly, do not paraphrase or omit it.
5. Give your final recommendation: trail name (EXACTLY as returned by search_trails), why it fits, weather outlook, and permit details.

CRITICAL:
- NEVER invent trail names, distances, elevation, or weather data. Use ONLY values returned by tools.
- NEVER write tool calls as text — actually invoke the tool.
- Commit to ONE trail. No lists, no options.
"""


def run(user_message: str, history: list[dict], accumulated_prefs: dict | None = None) -> tuple[str, list[dict], dict]:
    """
    Run one turn of the agent loop.

    Returns (response_text, updated_history, accumulated_preferences).
    accumulated_prefs carries preference state across turns.
    """
    client = OpenAI(base_url=OLLAMA_BASE, api_key="ollama")
    prefs = dict(accumulated_prefs or {})

    history = history + [{"role": "user", "content": user_message}]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    max_iterations = 10
    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            content = msg.content or ""
            if _NARRATED_TOOL_CALL_PATTERNS.search(content):
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "You described calling a tool but did not actually call it. "
                        "Do NOT write tool calls as text. Invoke the tool directly now."
                    ),
                })
                continue

            history.append({"role": "assistant", "content": content})
            return content, history, prefs

        # Execute each tool call
        messages.append(msg)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments)

            tool_fn = TOOL_MAP.get(fn_name)
            if tool_fn:
                result = tool_fn(**fn_args)
            else:
                result = f"Unknown tool: {fn_name}"

            # Capture set_preferences results immediately
            if result.startswith("PREFS_UPDATED:"):
                try:
                    partial = json.loads(result[len("PREFS_UPDATED:"):].strip())
                    prefs.update(partial)
                except Exception:
                    pass

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    history.append({"role": "assistant", "content": "I gathered the information but couldn't finalize. Please try rephrasing your request."})
    return history[-1]["content"], history, prefs
