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
MODEL = "mistral"  # swap to any model you have: `ollama list`

SYSTEM_PROMPT = """\
You are a friendly California backpacking trail advisor. Your job is to learn what the user wants through conversation, then find the perfect trail once they're ready.

## PHASE 1 — GATHERING PREFERENCES (default mode)

Your goal is to learn the user's trip preferences through natural conversation. Rules:
- Ask ONE short, friendly question per turn. Do not ask multiple questions at once.
- The questions you should eventually get answers to (in any order): WHERE (region/park), HOW HARD (difficulty), HOW LONG (miles or days), WHAT KIND (features like lakes, views, forests), WHAT SHAPE (loop, out-and-back, point-to-point).
- Every time the user reveals a preference, IMMEDIATELY call set_preferences with that info. Do not wait.
- After calling set_preferences, ask the next question naturally in your response.
- NEVER call search_trails, get_weather, get_permit_info, or get_trail_photo during this phase.
- You do NOT need all 5 preferences before moving to Phase 2. Use your judgment — if the user has given 3+ preferences and seems ready, proceed when they confirm.

## PHASE 2 — RECOMMENDING (only when user is ready)

Move to Phase 2 ONLY when:
- The user explicitly says they're ready (e.g. "find me a trail", "let's go", "yes", "sounds good", "book it", "show me trails"), OR
- The user has given enough preferences and you ask "Ready for me to find your trail?" and they confirm.

In Phase 2, call tools in this exact order:
1. search_trails — use all gathered preferences as filters
2. get_weather — use lat/lng from the best trail result
3. get_permit_info — use the area name
4. get_trail_photo — use trail name, lat, lng, and area name

CRITICAL — TRAIL NAME: Copy the trail name EXACTLY from search_trails output. Never invent a trail name.

CRITICAL — PHOTO: If get_trail_photo returns "PHOTO_URL: https://...", paste that ENTIRE line verbatim as the very first line of your response. Do NOT rewrite it.

Then write your recommendation: trail name (exactly from results), why it fits, weather outlook, permit details.

## GENERAL RULES
- NEVER call search_trails until the user is ready (Phase 2).
- NEVER list multiple trails. Commit to ONE.
- Keep responses short and conversational.
- If the user's very first message is casual chitchat with zero trail info (e.g. "hi", "hello", "let's find a trail"), greet them warmly and ask where in California they're thinking of going.
"""


def run(user_message: str, history: list[dict]) -> tuple[str, list[dict]]:
    """
    Run one turn of the agent loop.

    Returns the final assistant text response and the updated history.
    """
    client = OpenAI(base_url=OLLAMA_BASE, api_key="ollama")

    history = history + [{"role": "user", "content": user_message}]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    max_iterations = 10
    tools_called = False
    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        # No tool calls — check whether the model narrated a tool call as prose
        # (a known Mistral behaviour) instead of actually invoking it.  If so,
        # add a stern reminder and retry rather than surfacing the raw narration.
        if not msg.tool_calls:
            content = msg.content or ""
            if _NARRATED_TOOL_CALL_PATTERNS.search(content):
                # Append the bad response so the model sees it, then inject a
                # correction that tells it to call the tool for real this time.
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "You described calling a tool but did not actually call it. "
                        "Do NOT write tool calls as text. Invoke the tool directly now."
                    ),
                })
                continue  # retry the loop

            # Genuine final answer
            history.append({"role": "assistant", "content": content})
            return content, history

        # Execute each tool call
        tools_called = True
        messages.append(msg)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments)

            tool_fn = TOOL_MAP.get(fn_name)
            if tool_fn:
                result = tool_fn(**fn_args)
            else:
                result = f"Unknown tool: {fn_name}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Safety: if we hit max iterations, force a final answer
    history.append({"role": "assistant", "content": "I gathered the information but couldn't finalize. Please try rephrasing your request."})
    return history[-1]["content"], history
