"""
main.py — FastAPI entrypoint. Starts the background agent loop and serves
the dashboard (static frontend + api.py routes).

Run locally with: uvicorn main:app --reload
On Render, the start command should be: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import agent
import api
from browser import DEBUG_SCREENSHOT_PATH

app = FastAPI(title="X Reply Assistant")

app.include_router(api.router)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/debug/screenshot")
def debug_screenshot():
    """Open this URL directly in a browser to see exactly what X rendered the
    last time an interactive action (like, reply, compose) failed. Only shows
    up once something has actually failed — check Render logs for the label."""
    if os.path.exists(DEBUG_SCREENSHOT_PATH):
        return FileResponse(DEBUG_SCREENSHOT_PATH, media_type="image/png")
    return {"error": "No debug screenshot saved yet — nothing has failed since the last restart."}


@app.on_event("startup")
def startup():
    agent.start_background_thread()
