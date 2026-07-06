import logging
from openai import OpenAI
from utils.config import settings
from core.context_builder import RepoMapGenerator
from execution.sandbox import SandboxValidator
from services.github_client import PRService
from utils.config_manager import yaml_config
import json
import os
import time
from collections import deque
import asyncio

from .llm_client import LLMClient

logger = logging.getLogger(__name__)

llm_client = LLMClient()

# Shared state for duplicate-commit guard (used by webhook.py)
processing_commits: set = set()

async def run_agent_pipeline(repo_url: str, commit_sha: str, branch: str, run_id: int):
    exit_reason = "max_attempts"
    llm_client.reset_metrics()
    logger.info(f"Starting agent pipeline for {repo_url} on {branch} at {commit_sha}")
    start_time = time.time()

    # --- Duplicate PR Guard ---
    # Prevent infinite PR spam: if we already have an open PR for this branch,
    # don't start a new pipeline. The existing PR will be updated by the next push.
    try:
        from services.github_client import PRService as _PRS
        _pr_svc = _PRS()
        owner_repo = repo_url.replace("https://github.com/", "").replace(".git", "")
        owner, repo_name = owner_repo.split("/")
        existing_prs = _pr_svc.github.get_repo(f"{owner}/{repo_name}").get_pulls(
            state="open", head=f"{owner}:{branch}"
        )
        if existing_prs.totalCount > 0:
            logger.warning(f"Open PR already exists for branch '{branch}'. Skipping pipeline to prevent PR spam.")
            return False, [], llm_client.get_metrics()
    except Exception as e:
        logger.warning(f"Could not check for existing PRs: {e}. Proceeding anyway.")
    
    # 1. Parse Repo
    parser = RepoMapGenerator(repo_url, commit_sha)
    parser.clone_repo()
    repo_map = parser.generate_map()
    
    # 2. Setup Sandbox
    sandbox = SandboxValidator(parser.work_dir)
    await asyncio.to_thread(sandbox.start)
    
    # 3. Setup Agent tools
    files_modified = {}
    try:
        
        def get_repo_map():
            return repo_map
            
        def read_file_snippet(filepath: str, start_line: int, end_line: int):
            if not filepath:
                return "Error: filepath is required."
            target_path = os.path.join(parser.work_dir, filepath)
            if not os.path.exists(target_path):
                return f"Error: File {filepath} not found."
            with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            
            snippet = "".join(lines[max(0, start_line-1):end_line])
            return snippet
    
        def run_python_test_file(test_file: str):
            command = f"pytest {test_file}"
            return json.dumps(sandbox.execute_test(command))
            
        def apply_file_modification(filepath: str, content: str):
            sandbox.apply_patch(filepath, content)
            files_modified[filepath] = content
            return f"File {filepath} modified in sandbox."
    
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_repo_map",
                    "description": "Returns the architecture overview (classes and functions) of the repository.",
                    "parameters": {"type": "object", "properties": {}},
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file_snippet",
                    "description": "Reads specific lines of code from a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filepath": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"}
                        },
                        "required": ["filepath", "start_line", "end_line"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "run_python_test_file",
                    "description": "Runs tests using pytest in the sandbox. Pass a specific file or a folder (like '.' for all tests).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "test_file": {"type": "string", "description": "The path to the test file or folder to run"}
                        },
                        "required": ["test_file"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "apply_file_modification",
                    "description": "Applies a full file modification to the sandbox to be tested and eventually pushed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filepath": {"type": "string"},
                            "content": {"type": "string", "description": "The entirely new content of the file"}
                        },
                        "required": ["filepath", "content"]
                    }
                }
            }
        ]
    
        messages = [
            {"role": "system", "content": (
                "You are a CI/CD Resolution Agent. A GitHub Actions workflow failed. Investigate the failure, fix the code, and test it using run_python_test_file.\n\n"
                "Guidelines for efficiency:\n"
                "1. Be bold and make edits! Use `apply_file_modification` to rewrite files with your fixes.\n"
                "2. Call `get_repo_map` only once or twice to understand the project structure. Remember it and do not call it repeatedly.\n"
                "3. You MUST ensure ALL tests pass before submitting the PR by running the test suite (pass '.' to run_python_test_file). However, if you make purely non-functional changes (like documentation, comments, or logging), you may skip testing to save time.\n\n"
                "Output your final answer as a clean Markdown table with ONLY the following structure:\n\n"
                "| Root Cause | Fix |\n"
                "| --- | --- |\n"
                "| [Description of root cause] | [Description of fix] |"
            )}
        ]
    
        max_invocations = yaml_config["agent"].get("max_attempts", 10)
        choice = None
        # Stagnation detection: a rolling window of the last N call fingerprints
        recent_calls = deque(maxlen=3)
        
        for _ in range(max_invocations):
            try:
                response = await llm_client.generate(messages, tools=tools)
                logger.info("[API_CALL]")
                
                # Log token usage
                if hasattr(response, 'usage') and response.usage:
                    logger.info(f"[TOKEN_USAGE] {response.usage.prompt_tokens} {response.usage.completion_tokens}")
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                exit_reason = f"api_error: {e}"
                break
            
            if not response or not getattr(response, "choices", None):
                logger.error(f"No choices in response. Response: {response}")
                exit_reason = "empty_response"
                break
                
            choice = response.choices[0]
            
            # Log reasoning if available — handle both OpenAI and Gemini formats.
            # 'reasoning_details' is the Gemini/OpenRouter format.
            # Some models return reasoning as a separate 'reasoning' field.
            reasoning_text = None
            reasoning_encrypted = False
    
            if hasattr(choice.message, 'reasoning_details') and choice.message.reasoning_details:
                details = choice.message.reasoning_details
                for item in details:
                    item_type = item.get('type', '') if isinstance(item, dict) else getattr(item, 'type', '')
                    item_text = item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')
                    item_format = item.get('format', 'unknown') if isinstance(item, dict) else getattr(item, 'format', 'unknown')
                    if item_type in ('reasoning', 'reasoning.text', 'thinking') and item_text:
                        reasoning_text = item_text
                        reasoning_encrypted = (item_format == 'encrypted')
                        break
            elif hasattr(choice.message, 'reasoning') and choice.message.reasoning:
                reasoning_text = str(choice.message.reasoning)
                reasoning_encrypted = False
    
            if reasoning_text:
                escaped_reasoning = reasoning_text.replace('\n', '\\n')
                logger.info(f"[AGENT_REASONING] encrypted={reasoning_encrypted}\\n{escaped_reasoning}")
    
            messages.append(choice.message)
            if choice.message.content:
                logger.info(f"[STREAM_CHUNK]{choice.message.content.replace('\n', '\\\\n')}")
            
            if choice.message.tool_calls:
                # --- Stagnation Detector ---
                # Track last N (fn_name, args_str, result_str) tuples.
                # If all identical → the agent is stuck. Abort early.
                if not hasattr(run_agent_pipeline, '_stagnation_buffer'):
                    pass  # initialised below per-call
                stagnation_window = 3
    
                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                        
                    logger.info(f"LLM Tool Call: {fn_name}")
                    
                    if fn_name == "get_repo_map":
                        res = get_repo_map()
                    elif fn_name == "read_file_snippet":
                        res = read_file_snippet(args.get("filepath"), args.get("start_line", 1), args.get("end_line", 100))
                    elif fn_name == "run_python_test_file":
                        res = await asyncio.to_thread(run_python_test_file, args.get("test_file"))
                    elif fn_name == "apply_file_modification":
                        res = apply_file_modification(args.get("filepath"), args.get("content"))
                    else:
                        res = "Unknown tool."
    
                    # Stagnation check: track (fn_name, serialised args, serialised result)
                    call_fingerprint = (fn_name, json.dumps(args, sort_keys=True), str(res)[:200])
                    recent_calls.append(call_fingerprint)
                    if len(recent_calls) >= stagnation_window and len(set(recent_calls)) == 1:
                        logger.error(
                            f"[STAGNATION DETECTED] Agent called '{fn_name}' {stagnation_window} times "
                            f"with identical args and output. Aborting loop."
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(res)
                        })
                        # Force exit from the main loop
                        exit_reason = "stagnation"
                        choice = None
                        break
                        
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(res)
                    })
                
                if choice is None:  # stagnation triggered
                    break
            else:
                exit_reason = "success"
                break
    
        final_content = choice.message.content if (choice and choice.message.content) else ""
        if not final_content or not final_content.strip() or "| Root Cause |" not in final_content:
            if exit_reason == "success":
                final_content = (
                    "| Root Cause | Fix |\n"
                    "| --- | --- |\n"
                    "| unknown | Pipeline completed successfully but no final summary was generated. |"
                )
            elif exit_reason.startswith("api_error"):
                err_msg = exit_reason.split(":", 1)[1].strip() if ":" in exit_reason else "LLM call failed"
                final_content = (
                    "| Root Cause | Fix |\n"
                    "| --- | --- |\n"
                    f"| Late LLM / API error ({err_msg}) | Fix was successfully applied in sandbox. |"
                )
            elif exit_reason == "stagnation":
                final_content = (
                    "| Root Cause | Fix |\n"
                    "| --- | --- |\n"
                    "| stagnation | Agent execution stagnated (repeated tool calls). |"
                )
            else:
                final_content = (
                    "| Root Cause | Fix |\n"
                    "| --- | --- |\n"
                    f"| exit reason: {exit_reason} | Agent reached max attempts without producing a final analysis. |"
                )
        logger.info(f"Final Answer:\n{final_content}")
        
        # Synchronize files_modified with the actual files modified on disk (via git status)
        # to catch any out-of-band updates (e.g. from shell/cat commands).
        import subprocess
        try:
            git_status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=parser.work_dir,
                text=True
            )
            for line in git_status.splitlines():
                line = line.strip()
                if not line:
                    continue
                status_code = line[:2]
                filepath_git = line[3:].strip('"')
                # Handle modifications, additions, and untracked files
                if any(code in status_code for code in ["M", "A", "??"]):
                    # Exclude compiled files, cache directories, and semgrep configs/caches
                    if (
                        "__pycache__" in filepath_git
                        or filepath_git.endswith(".pyc")
                        or filepath_git.startswith(".pytest_cache")
                        or filepath_git.startswith(".ruff_cache")
                        or filepath_git.startswith(".semgrep")
                    ):
                        continue
                    full_path = os.path.join(parser.work_dir, filepath_git)
                    if os.path.isfile(full_path):
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f_git:
                            files_modified[filepath_git] = f_git.read()
                        logger.info(f"Refreshed modified file from disk: {filepath_git}")
                # Handle deleted files
                elif "D" in status_code:
                    if filepath_git in files_modified:
                        del files_modified[filepath_git]
                        logger.info(f"Detected deleted file from git status: {filepath_git}")
        except Exception as e:
            logger.error(f"Failed to auto-detect modified files via git status: {e}")

        # 6. Commit & Push
        if files_modified:
            pr_service = PRService()
            await asyncio.to_thread(pr_service.push_fix, repo_url, branch, files_modified, final_content, commit_sha)
            logger.info("Pipeline completed successfully.")
            success = True
        else:
            logger.warning("No files were modified by the agent.")
            success = False
            
        duration = time.time() - start_time
        logger.info(f"Pipeline finished in {duration:.2f} seconds.")
        return success, messages, llm_client.get_metrics()
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        logger.info(f"Pipeline completed. (Failed due to error: {e})")
        return False, [], llm_client.get_metrics()
    finally:
        await asyncio.to_thread(sandbox.stop)
