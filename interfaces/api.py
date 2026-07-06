from fastapi import FastAPI
import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interfaces.webhook import router as webhook_router

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="co_resolve CI/CD Resolution Agent")

app.include_router(webhook_router, prefix="/api")

@app.get("/")
def health_check():
    return {"status": "ok", "agent": "co_resolve"}
