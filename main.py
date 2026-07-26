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

app = FastAPI(title="X Reply Assistant")

app.include_router(api.router)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.on_event("startup")
def startup():
    agent.start_background_thread()
