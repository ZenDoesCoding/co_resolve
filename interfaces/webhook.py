import hmac
import hashlib
from fastapi import APIRouter, Request, Header, HTTPException, BackgroundTasks
from starlette.requests import ClientDisconnect
from typing import Optional
from utils.config import settings
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from core.orchestrator import run_agent_pipeline

router = APIRouter()

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not signature_header:
        return False
    hash_object = hmac.new(settings.github_webhook_secret.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected_signature, signature_header)

@router.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None)
):
    try:
        payload_body = await request.body()
    except ClientDisconnect:
        logger.warning("Client disconnected while reading request body")
        raise HTTPException(status_code=400, detail="Client disconnected")
    
    if not verify_signature(payload_body, x_hub_signature_256):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    if x_github_event != "workflow_run":
        return {"status": "ignored", "reason": f"Event {x_github_event} not handled"}
        
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = data.get("action")
    workflow_run = data.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion")

    logger.info(f"Webhook received: event={x_github_event}, action={action}, conclusion={conclusion}")

    if action == "completed" and conclusion == "failure":
        repo_url = data.get("repository", {}).get("clone_url")
        commit_sha = workflow_run.get("head_sha")
        branch = workflow_run.get("head_branch")
        run_id = workflow_run.get("id")
        
        if branch and branch.startswith("fix/agent-resolution-"):
            logger.info(f"Ignoring webhook from agent's own PR branch: {branch}")
            return {"status": "ignored", "reason": "Webhook from agent's own PR branch"}
            
        from core.orchestrator import processing_commits
        if commit_sha in processing_commits:
            logger.info(f"Pipeline already running for commit {commit_sha}. Skipping scheduling.")
            return {"status": "ignored", "reason": "Pipeline already running"}
            
        logger.info(f"Detected failed workflow `{run_id}` on `{repo_url}` commit `{commit_sha}` branch `{branch}`")
        
        # Trigger the agent pipeline asynchronously
        background_tasks.add_task(run_agent_pipeline, repo_url, commit_sha, branch, run_id)
        
        return {"status": "accepted", "message": "Resolution agent started"}

    return {"status": "ignored", "reason": "Workflow did not fail or is not completed"}

@router.post("/hitl")
async def set_hitl_action(payload: dict):
    action = payload.get("action")
    if action in ("approve", "reject"):
        import core.orchestrator
        core.orchestrator.hitl_action = action
        # Also sync it to the currently running architecture module if loaded
        from utils.active_arc import get_active_arc
        import importlib
        try:
            arc_num = get_active_arc()
            arc_map = {
                1: "basic_arc",
                2: "openai_arc",
                3: "google_arc"
            }
            arc_name = arc_map.get(arc_num, f"arc_{arc_num}")
            module_name = f"architectures.{arc_name}.core.orchestrator"
            module = importlib.import_module(module_name)
            module.hitl_action = action
        except Exception:
            pass
        logger.info(f"Set HitL action to {action} in backend process")
        return {"status": "success", "action": action}
    return {"status": "error", "message": "Invalid action"}

@router.post("/switch_arc")
async def switch_arc(payload: dict):
    arc = payload.get("arc")
    if arc in (1, 2, 3):
        from utils.active_arc import set_active_arc
        set_active_arc(arc)
        logger.info(f"Switched active backend to ARC {arc} via API")
        return {"status": "success", "active_arc": arc}
    return {"status": "error", "message": "Invalid arc"}

@router.get("/active_arc")
async def active_arc():
    from utils.active_arc import get_active_arc
    return {"active_arc": get_active_arc()}
