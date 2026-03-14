"""
FastAPI server wrapping the trail advisor agent for the web frontend.
"""

import json
import re
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pathlib import Path
import agent
from data import get_trails

app = FastAPI()

# In-memory session histories (keyed by session_id)
_sessions: dict[str, list[dict]] = {}

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/trails")
async def list_trails():
    """Return all trails for map/browse features."""
    trails = get_trails()
    return trails


@app.get("/api/trails/stats")
async def trail_stats():
    """Return summary stats."""
    trails = get_trails()
    areas = len({t["area"] for t in trails})
    difficulties = {}
    for t in trails:
        d = t["difficulty"]
        difficulties[d] = difficulties.get(d, 0) + 1
    return {
        "total_trails": len(trails),
        "total_areas": areas,
        "difficulties": difficulties,
    }


@app.post("/api/chat")
async def chat(request: Request):
    """Send a message to the agent and get a response."""
    body = await request.json()
    user_message = body.get("message", "")
    session_id = body.get("session_id", "default")

    history = _sessions.get(session_id, [])

    try:
        response_text, history = agent.run(user_message, history)
        _sessions[session_id] = history

        # Extract PHOTO_URL: <url> from the response text, if present.
        # Match both "PHOTO_URL: https://..." and edge cases like "**PHOTO_URL:** https://..."
        photo_url = None
        photo_match = re.search(r"PHOTO_URL[:\*\s]+\s*(https?://\S+)", response_text, re.IGNORECASE)
        if photo_match:
            photo_url = photo_match.group(1).rstrip(")")  # strip any trailing paren from markdown
            # Strip the entire PHOTO_URL line from the visible text
            response_text = re.sub(r"\*{0,2}PHOTO_URL[:\*\s]+\s*https?://\S+\*{0,2}\s*", "", response_text, flags=re.IGNORECASE).strip()

        # Extract the latest PREFS_UPDATED from the full message history
        # (set_preferences tool results are stored in the history)
        current_prefs = {}
        for msg in history:
            if isinstance(msg, dict) and msg.get("role") == "tool":
                content = msg.get("content", "")
                if content.startswith("PREFS_UPDATED:"):
                    try:
                        import json as _json
                        partial = _json.loads(content[len("PREFS_UPDATED:"):].strip())
                        current_prefs.update(partial)
                    except Exception:
                        pass

        payload = {"response": response_text, "session_id": session_id, "preferences": current_prefs}
        if photo_url:
            payload["photo_url"] = photo_url
        return payload
    except Exception as e:
        return {"error": str(e), "response": f"Agent error: {e}"}


@app.post("/api/chat/reset")
async def reset_chat(request: Request):
    """Reset conversation history."""
    body = await request.json()
    session_id = body.get("session_id", "default")
    _sessions.pop(session_id, None)
    return {"status": "ok"}


# Serve static files last (catch-all)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6767)
