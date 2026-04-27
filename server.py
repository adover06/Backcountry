"""FastAPI server wrapper for the trip planner API."""

import uvicorn
from fastapi import FastAPI

from planner_api import router as planner_router

app = FastAPI()
app.include_router(planner_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6767)
