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

# Global state for HitL (Human-in-the-Loop)
hitl_action = None
processing_commits = set()

async def run_agent_pipeline(repo_url: str, commit_sha: str, branch: str, run_id: int):
    # Already checked and managed by root router
    processing_commits.add(commit_sha)
    try:
        llm_client.reset_metrics()
        logger.info(f"Starting agent pipeline for {repo_url} on {branch} at {commit_sha}")
        start_time = time.time()

        # --- Duplicate PR Guard ---
        # Prevent infinite PR spam: if we already have an open PR for this branch,
        # don't start a new pipeline. The existing PR will be updated by the next push.
        try:
            import asyncio
            from services.github_client import PRService as _PRS
            
            def check_prs():
                _pr_svc = _PRS()
                owner_repo = repo_url.replace("https://github.com/", "").replace(".git", "")
                owner, repo_name = owner_repo.split("/")
                return _pr_svc.github.get_repo(f"{owner}/{repo_name}").get_pulls(
                    state="open", head=f"{owner}:{branch}"
                ).totalCount

            total_prs = await asyncio.to_thread(check_prs)
            if total_prs > 0:
                logger.warning(f"Open PR already exists for branch '{branch}'. Skipping pipeline to prevent PR spam.")
                logger.info("Pipeline completed.") # This notifies the TUI to go Idle!
                return False, [], llm_client.get_metrics()
        except Exception as e:
            logger.warning(f"Could not check for existing PRs: {e}. Proceeding anyway.")
        
        # 0. Load Knowledge Base
        from core.knowledge_manager import KnowledgeManager
        kb = KnowledgeManager()
        knowledge_prompt = kb.format_for_prompt()
        
        # 1. Parse Repo
        parser = RepoMapGenerator(repo_url, commit_sha)
        await asyncio.to_thread(parser.clone_repo)
        repo_map = await asyncio.to_thread(parser.generate_map)
        
        # 2. Setup Sandbox
        sandbox = SandboxValidator(parser.work_dir)
        await asyncio.to_thread(sandbox.start)
        await asyncio.to_thread(sandbox.execute_test, "pip install ruff")
        await asyncio.to_thread(sandbox.execute_test, "pip install semgrep")
        try:
        
            # 3. Setup Agent tools
            files_modified = {}
        
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
            
                # Enforce max 250 lines per snippet to prevent context bloat
                original_requested_lines = end_line - start_line
                if original_requested_lines > 250:
                    end_line = start_line + 250
                    
                snippet = "".join(lines[max(0, start_line-1):end_line])
                if original_requested_lines > 250:
                    snippet += "\n\n...[SNIPPET TRUNCATED TO 250 LINES TO SAVE TOKENS. CALL AGAIN FOR MORE]...\n"
                return snippet

            def run_python_test_file(test_file: str):
                if test_file and test_file != ".":
                    if test_file.endswith(".py"):
                        dir_name = os.path.dirname(test_file)
                        file_name = os.path.basename(test_file)
                        if dir_name:
                            command = f"cd {dir_name} && pytest {file_name} -q --tb=short --no-header"
                        else:
                            command = f"pytest {file_name} -q --tb=short --no-header"
                    else:
                        # Assume it's a directory
                        command = f"cd {test_file} && pytest . -q --tb=short --no-header"
                else:
                    command = f"pytest . -q --tb=short --no-header"
                    
                result = sandbox.execute_test(command)
                # Truncate logs to prevent context bloat
                log_str = result.get("logs", "")
                if len(log_str) > 1500:
                    result["logs"] = "\n\n...[TRUNCATED BY SYSTEM TO SAVE TOKENS]...\n\n" + log_str[-1500:]
                return json.dumps(result)
            
            def run_shell_command(command: str):
                result = sandbox.execute_test(command)
                log_str = result.get("logs", "")
                if len(log_str) > 2000:
                    result["logs"] = log_str[:1000] + "\n\n...[TRUNCATED]...\n\n" + log_str[-1000:]
                return json.dumps(result)
            
            def run_code_audit(target: str):
                command = f"ruff check {target}"
                result = sandbox.execute_test(command)
                # Truncate logs if too long
                log_str = result.get("logs", "")
                if len(log_str) > 2000:
                    result["logs"] = log_str[:1000] + "\n\n...[TRUNCATED]...\n\n" + log_str[-1000:]
                return json.dumps(result)
            
            def run_semgrep_audit(target: str):
                command = f"semgrep scan --config=/semgrep_rules/python {target}"
                result = sandbox.execute_test(command)
                # Truncate logs if too long
                log_str = result.get("logs", "")
                if len(log_str) > 2000:
                    result["logs"] = log_str[:1000] + "\n\n...[TRUNCATED]...\n\n" + log_str[-1000:]
                return json.dumps(result)
            
            def multi_replace_file_content(filepath: str, replacements: list):
                target_path = os.path.join(parser.work_dir, filepath)
                if not os.path.exists(target_path):
                    # If it doesn't exist, we might be creating it. Handled by apply_patch.
                    pass
                else:
                    with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    
                    import json
                    valid_reps = []
                    for rep in replacements:
                        if isinstance(rep, str):
                            try:
                                rep = json.loads(rep)
                            except:
                                continue
                        if isinstance(rep, dict):
                            valid_reps.append(rep)
                            
                    sorted_reps = sorted(valid_reps, key=lambda x: x.get('start_line', 1), reverse=True)
                    for rep in sorted_reps:
                        start_idx = max(0, rep.get('start_line', 1) - 1)
                        end_idx = rep.get('end_line', len(lines))
                        new_content = rep.get('content', '')
                        if not new_content.endswith('\n'):
                            new_content += '\n'
                        lines[start_idx:end_idx] = [new_content]
                
                    new_full_content = "".join(lines)
                    sandbox.apply_patch(filepath, new_full_content)
                    files_modified[filepath] = new_full_content
                    return f"File {filepath} successfully updated."
                
                # Fallback if file doesn't exist (creating new file)
                if replacements and len(replacements) == 1:
                    sandbox.apply_patch(filepath, replacements[0].get('content', ''))
                    files_modified[filepath] = replacements[0].get('content', '')
                    return f"Created new file {filepath}."
                return f"Error: Cannot multi-replace on non-existent file {filepath}. Provide single replacement block to create."

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
                        "name": "run_code_audit",
                        "description": "Runs a code audit using Ruff to find security flaws and bugs. Pass '.' for all files.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "The file or folder to audit (e.g. '.' or 'test2/validator.py')"}
                            },
                            "required": ["target"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_semgrep_audit",
                        "description": "Runs a deep security audit using Semgrep to find complex security flaws (like timing attacks). Pass '.' for all files.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "The file or folder to audit (e.g. '.' or 'test4/auth.py')"}
                            },
                            "required": ["target"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "multi_replace_file_content",
                        "description": "Precisely modifies an existing file by replacing multiple blocks of code. Do not rewrite the whole file, only the parts you need to change. If you want to create a new file, pass a single replacement starting at line 1.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "filepath": {"type": "string", "description": "Path to the file to modify."},
                                "replacements": {
                                    "type": "array",
                                    "description": "A list of objects specifying the start_line, end_line, and the new content to drop in.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "start_line": {"type": "integer", "description": "The 1-indexed start line to replace (inclusive)."},
                                            "end_line": {"type": "integer", "description": "The 1-indexed end line to replace (inclusive)."},
                                            "content": {"type": "string", "description": "The replacement code block."}
                                        },
                                        "required": ["start_line", "end_line", "content"]
                                    }
                                }
                            },
                            "required": ["filepath", "replacements"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_shell_command",
                        "description": "Runs an arbitrary shell command in the sandbox. Use this to inspect the environment, run custom scripts, or check module resolution.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "The shell command to run."}
                            },
                            "required": ["command"]
                        }
                    }
                }
            ]
            
            if yaml_config["agent"].get("turbo_mode", False):
                tools.append({
                    "type": "function",
                    "function": {
                        "name": "add_knowledge_candidate",
                        "description": "Record a new pattern you discovered. ONLY use this for hyper-specific fixes that are not found by tests or Semgrep, or for subtle bugs (like mutable defaults).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "pattern": {"type": "string", "description": "What the code looks like (the symptom)"},
                                "problem": {"type": "string", "description": "Why it's wrong (the diagnosis)"},
                                "solution": {"type": "string", "description": "How to fix it (the prescription)"}
                            },
                            "required": ["pattern", "problem", "solution"]
                        }
                    }
                })

            logger.info("Running baseline tests for predictive context...")
            baseline_result = await asyncio.to_thread(sandbox.execute_test, "pytest")
            predictive_ctx = ""
            if baseline_result["exit_code"] != 0:
                logger.info("Extracting predictive context from failed baseline tests...")
                predictive_ctx = parser.get_predictive_context(baseline_result["logs"])

            system_prompt = (
                "You are a highly skilled CI/CD Resolution and Security Agent. A GitHub Actions workflow failed, and there may also be hidden security flaws in the codebase.\n\n"
                "### Your Mission\n"
                "1. **Fix ALL Failing Tests**: You must investigate and fix all failing tests. Do not stop after fixing just one if others are still broken.\n"
                "2. **Find and Fix Security Flaws**: Actively look for security vulnerabilities in the code you examine (e.g., mutable default arguments used insecurely, hardcoded secrets, injection risks, insecure imports). Fix them even if they didn't cause the test failure directly, but mention them in your report.\n"
                "3. **Ensure Green CI**: Your goal is to make all tests pass in the sandbox before finishing.\n\n"
                "### Workflow Strategy\n"
                "1. **Investigate**: Run tests using `run_python_test_file` to see the failures. The logs are truncated, so pay attention to the specific files and line numbers mentioned.\n"
                "2. **Audit**: Use `run_code_audit` (Ruff) for quick checks AND `run_semgrep_audit` for deep security analysis to find flaws (like timing attacks) or bugs.\n"
                "3. **Read**: Use `read_file_snippet` on the failing source code files. DO NOT guess the code. You must read it first.\n"
                "4. **Edit**: Use `multi_replace_file_content` to make surgical, precise edits to the logic. Do not rewrite the whole file.\n"
                "5. **Verify**: Run `run_python_test_file` (pass '.') to ensure your fixes work before submitting.\n"
                "6. **Debug**: Use `run_shell_command` to inspect the environment, run custom scripts, or check module resolution if you encounter import issues.\n\n"
                "⚠️ CRITICAL: You MUST run both `run_code_audit` and `run_semgrep_audit` at least once before submitting your final answer! If you try to finish without running them, your answer will be rejected. ⚠️\n\n"
                "### Critical Rules\n"
                "- **NEVER** edit configuration files like `conftest.py`, `pytest.ini`, or `sitecustomize.py` unless the bug is explicitly a configuration issue. The bug is almost certainly in the source code.\n"
                "- **ABSOLUTELY FORBIDDEN**: Do NOT use hacky fixes to make tests pass. Do NOT place functions in unrelated modules just because Python's import system resolves it there first! You MUST fix the problem in the file where it originates, even if it requires more effort!\n"
                "- **ABSOLUTELY FORBIDDEN**: Do NOT ignore security vulnerabilities (like timing attacks) just because you can make tests pass by other means. You MUST fix them in the file where they exist!\n"
                "- **STAGNATION WARNING**: Do NOT repeat the exact same tool call with the same arguments if it failed or didn't yield new results. Try a different approach or modify the arguments! If you repeat yourself, the system will abort your run!\n"
                "- Call `get_repo_map` only once if you need an overview. Do not spam it.\n\n"
                "### Action-Oriented Execution\n"
                "- **Be Concise**: Keep your internal reasoning short and focused on the next actionable step. Do NOT write long paragraphs simulating code execution or repeating already known facts.\n"
                "- **Batch Requests**: If you need to read or inspect multiple files, emit multiple parallel tool calls in a single turn instead of doing them sequentially!\n"
                "- **Don't Spam Tools**: Do not call tools with tiny overlapping ranges or identical arguments. Read larger chunks or the whole file if needed.\n"
                "- **Execute Decisively**: Once you identify a bug, fix it! Do not over-analyze or doubt yourself in endless loops.\n\n"
            )
            
            if predictive_ctx:
                system_prompt += f"{predictive_ctx}\n\n"
                
            if knowledge_prompt:
                system_prompt += f"{knowledge_prompt}\n\n"
                
            system_prompt += (
                "Output your final answer as a clean Markdown table with ONLY the following structure:\n\n"
                "| Root Cause | Fix |\n"
                "| --- | --- |\n"
                "| [Description of root cause] | [Description of fix] |"
            )

            messages = [{"role": "system", "content": system_prompt}]

            max_invocations = yaml_config["agent"].get("max_attempts", 10)
            choice = None
            exit_reason = "max_attempts"
            # Stagnation detection: a rolling window of the last N call fingerprints
            recent_calls = deque(maxlen=3)
            ruff_called = False
            semgrep_called = False
            audit_reminders = 0
            empty_turn_count = 0
        
            for _ in range(max_invocations):
                # --- Context Pruning ---
                # To prevent massive token bloat, we prune old tool outputs.
                # We keep the system prompt (index 0) and the most recent few messages untouched.
                if len(messages) > 4:
                    # We iterate up to the third to last message to leave recent context intact.
                    for msg in messages[1:-2]:
                        if isinstance(msg, dict) and msg.get("role") == "tool":
                            content = str(msg.get("content", ""))
                            if len(content) > 1000:
                                msg["content"] = content[:500] + "\n\n...[TRUNCATED PREVIOUS TOOL OUTPUT TO SAVE TOKENS]...\n"

                try:
                    use_stream = True
                    response = await llm_client.generate(messages, tools=tools, stream=use_stream)
                
                    tool_calls_dict = {}
                    content = ""
                    reasoning = ""
                
                    class AttrDict(dict):
                        def __init__(self, *args, **kwargs):
                            super(AttrDict, self).__init__(*args, **kwargs)
                            self.__dict__ = self
 
                    if use_stream:
                        async for chunk in response:
                            if not chunk or not getattr(chunk, "choices", None):
                                continue
                            delta = chunk.choices[0].delta
                        
                            # Yield text to TUI
                            if delta.content:
                                content += delta.content
                                logger.info(f"[STREAM_CHUNK]{delta.content.replace('\n', '\\n')}")
                        
                            # OpenRouter / Anthropic reasoning
                            if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                                reasoning += delta.reasoning_content
                            elif hasattr(delta, 'reasoning') and delta.reasoning:
                                reasoning += delta.reasoning
                        
                            if delta.tool_calls:
                                for tc_chunk in delta.tool_calls:
                                    idx = tc_chunk.index
                                    if idx not in tool_calls_dict:
                                        tool_calls_dict[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                                    if tc_chunk.id:
                                        tool_calls_dict[idx]["id"] += tc_chunk.id
                                    if tc_chunk.function.name:
                                        tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                                    if tc_chunk.function.arguments:
                                        tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments
                                    if hasattr(tc_chunk, 'model_extra') and tc_chunk.model_extra:
                                        if "extra" not in tool_calls_dict[idx]:
                                            tool_calls_dict[idx]["extra"] = {}
                                        tool_calls_dict[idx]["extra"].update(tc_chunk.model_extra)
                                    
                        logger.info("[API_CALL]")
                    
                        tool_calls = []
                        for idx in sorted(tool_calls_dict.keys()):
                            tc = tool_calls_dict[idx]
                            tc_obj = AttrDict({
                                "id": tc["id"],
                                "type": "function",
                                "function": AttrDict({
                                    "name": tc["function"]["name"],
                                    "arguments": tc["function"]["arguments"]
                                })
                            })
                            if "extra" in tc:
                                tc_obj.update(tc["extra"])
                            tool_calls.append(tc_obj)
                    else:
                        # Non-streaming fallback
                        if not response or not getattr(response, "choices", None):
                            logger.error(f"No choices in response.")
                            exit_reason = "empty_response"
                            break
                        resp_choice = response.choices[0]
                        content = resp_choice.message.content or ""
                        reasoning = getattr(resp_choice.message, 'reasoning_content', "") or getattr(resp_choice.message, 'reasoning', "") or ""
                        
                        if content:
                            logger.info(f"[STREAM_CHUNK]{content.replace('\n', '\\n')}")
                        
                        tool_calls = []
                        if resp_choice.message.tool_calls:
                            for tc in resp_choice.message.tool_calls:
                                tc_obj = AttrDict({
                                    "id": tc.id,
                                    "type": "function",
                                    "function": AttrDict({
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments
                                    })
                                })
                                if hasattr(tc, 'model_extra') and tc.model_extra:
                                    tc_obj.update(tc.model_extra)
                                tool_calls.append(tc_obj)
                        logger.info("[API_CALL] (Non-streaming)")
 
                    choice = AttrDict({
                        "message": AttrDict({
                            "content": content if content else None,
                            "tool_calls": tool_calls if tool_calls else None,
                            "role": "assistant",
                            "steps": getattr(response, "steps", None)
                        })
                    })
                
                    if reasoning:
                        escaped_reasoning = reasoning.replace('\n', '\\n')
                        logger.info(f"[AGENT_REASONING] encrypted=False\\n{escaped_reasoning}")

                except Exception as e:
                    logger.error(f"LLM call failed: {e}")
                    exit_reason = f"api_error: {e}"
                    break
            
                if not choice:
                    logger.error(f"No choices in response.")
                    exit_reason = "empty_response"
                    break

                messages.append(choice.message)
            
                if choice.message.tool_calls:
                    empty_turn_count = 0
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
                        elif fn_name == "run_shell_command":
                            res = await asyncio.to_thread(run_shell_command, args.get("command"))
                        elif fn_name == "run_code_audit":
                            res = await asyncio.to_thread(run_code_audit, args.get("target", "."))
                            ruff_called = True
                        elif fn_name == "run_semgrep_audit":
                            res = await asyncio.to_thread(run_semgrep_audit, args.get("target", "."))
                            semgrep_called = True
                        elif fn_name == "multi_replace_file_content":
                            res = multi_replace_file_content(args.get("filepath"), args.get("replacements", []))
                        elif fn_name == "add_knowledge_candidate":
                            res = kb.add_candidate(
                                args.get("pattern", ""),
                                args.get("problem", ""),
                                args.get("solution", ""),
                                repo_url,
                                commit_sha
                            )
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
                                "name": fn_name,
                                "content": str(res)
                            })
                            # Force exit from the main loop
                            exit_reason = "stagnation"
                            choice = None
                            break
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fn_name,
                            "content": str(res)
                        })
                        
                        if settings.llm_provider == "google":
                            if 'next_inputs' not in locals():
                                next_inputs = []
                            next_inputs.append({
                                "type": "function_result",
                                "name": tc.function.name,
                                "call_id": tc.id,
                                "result": [{"type": "text", "text": str(res)}]
                            })
                
                    if settings.llm_provider == "google" and 'next_inputs' in locals():
                        next_input = next_inputs
                        del next_inputs # Clear for next turn
                        
                    if choice is None:  # stagnation triggered
                        break
                else:
                    # Agent produced no tool calls.
                    # Check if it actually tried to provide the final answer table.
                    has_final_answer = choice.message.content and "| Root Cause |" in choice.message.content
                    
                    if has_final_answer and (not ruff_called or not semgrep_called):
                        if audit_reminders < 3:
                            audit_reminders += 1
                            reminder_msg = "⚠️ CRITICAL: You attempted to finish without running the required audits! "
                            if not ruff_called:
                                reminder_msg += "You must run `run_code_audit` (Ruff). "
                            if not semgrep_called:
                                reminder_msg += "You must run `run_semgrep_audit` (Semgrep). "
                            reminder_msg += "Please run the missing audits before submitting your final answer."
                            
                            logger.warning(f"Deterministic Gate: Agent forgot audits. Sending reminder {audit_reminders}/3.")
                            messages.append({
                                "role": "user",
                                "content": reminder_msg
                            })
                            continue
                        else:
                            logger.error("Deterministic Gate: Agent failed to run audits after 3 reminders. Aborting.")
                            exit_reason = "forgot_audits"
                            choice = None
                            break
                    elif not has_final_answer:
                        empty_turn_count += 1
                        if empty_turn_count > 5:
                            logger.error("Agent produced no tool calls and no final answer for 5 consecutive turns. Aborting to prevent infinite loop.")
                            exit_reason = "empty_turns"
                            choice = None
                            break
                        logger.warning("Agent produced no tool calls and no final answer. Prompting to continue.")
                        messages.append({
                            "role": "user",
                            "content": "⚠️ You did not call any tools in this turn. If you are still investigating or fixing, you MUST call a tool (e.g., `read_file_snippet`, `run_shell_command`). If you are finished, you MUST output the final answer table."
                        })
                        continue
                    else:
                        # Has final answer and audits were called
                        exit_reason = "success"
                        break

            final_content = choice.message.content if choice else ""
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
                elif exit_reason == "forgot_audits":
                    final_content = (
                        "| Root Cause | Fix |\n"
                        "| --- | --- |\n"
                        "| missing audits | Agent failed to run required Ruff/Semgrep audits. |"
                    )
                elif exit_reason == "empty_turns":
                    final_content = (
                        "| Root Cause | Fix |\n"
                        "| --- | --- |\n"
                        "| empty turns | Agent produced consecutive empty turns. |"
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
                    filepath = line[3:].strip('"')
                    # Handle modifications, additions, and untracked files
                    if any(code in status_code for code in ["M", "A", "??"]):
                        # Exclude compiled files, cache directories, and semgrep configs/caches
                        if (
                            "__pycache__" in filepath
                            or filepath.endswith(".pyc")
                            or filepath.startswith(".pytest_cache")
                            or filepath.startswith(".ruff_cache")
                            or filepath.startswith(".semgrep")
                        ):
                            continue
                        full_path = os.path.join(parser.work_dir, filepath)
                        if os.path.isfile(full_path):
                            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                files_modified[filepath] = f.read()
                            logger.info(f"Refreshed modified file from disk: {filepath}")
                    # Handle deleted files
                    elif "D" in status_code:
                        if filepath in files_modified:
                            del files_modified[filepath]
                            logger.info(f"Detected deleted file from git status: {filepath}")
            except Exception as e:
                logger.error(f"Failed to auto-detect modified files via git status: {e}")

            # --- Mandatory Pre-PR Verification Gate ---
            # Before pushing anything, we do one final full test run.
            # If tests are still failing, we do NOT create a PR with a broken fix.
            if files_modified:
                logger.info("Running mandatory pre-PR verification...")
                
                # Extract unique top-level directories from modified files
                dirs_to_test = set()
                for filepath in files_modified:
                    parts = filepath.split("/")
                    if len(parts) > 1:
                        dirs_to_test.add(parts[0])
                    else:
                        dirs_to_test.add(".")
                
                all_passed = True
                for test_dir in dirs_to_test:
                    if test_dir == ".":
                        command = "PYTHONPATH=. pytest . -q --tb=short --no-header"
                    else:
                        command = f"PYTHONPATH=. pytest {test_dir} -q --tb=short --no-header"
                    
                    logger.info(f"Running verification in `{test_dir}`...")
                    verification_result = await asyncio.to_thread(sandbox.execute_test, command)
                    if verification_result["exit_code"] != 0:
                        all_passed = False
                        logger.error(f"Verification FAILED in `{test_dir}`.")
                        logger.warning(f"Logs:\n{verification_result.get('logs', '')}")
                        break
                        
                if not all_passed:
                    logger.error("Pre-PR verification FAILED. Tests are still red. Aborting PR creation to prevent a bad fix.")
                    logger.warning("No files were pushed. The agent could not fully resolve the issue.")
                    logger.info("Pipeline completed. (Failed at verification)")
                    return False, messages, llm_client.get_metrics()
                    
                logger.info("Pre-PR verification PASSED. All affected directories green. Proceeding to push.")
                
                # Check for pending knowledge candidates
                pending = kb.promote_candidates()
                if pending:
                    logger.info(f"[KNOWLEDGE_REVIEW]{json.dumps(pending)}")
                    
                global hitl_action
                hitl_enabled = False  # Disabled for benchmark all the time
            
                if hitl_enabled:
                    timeout = yaml_config.get("agent", {}).get("hitl_timeout_seconds", 30)
                    logger.info(f"HitL: Fix is ready. Waiting up to {timeout}s for manual approval. Type /approve to push now, /reject to abort.")
                    hitl_action = None
                
                    # Smart Timeout Loop
                    start_wait = time.time()
                    while time.time() - start_wait < timeout:
                        if hitl_action == "approve":
                            logger.info("HitL: User approved the fix.")
                            break
                        elif hitl_action == "reject":
                            logger.info("HitL: User rejected the fix. Aborting.")
                            return False, messages, llm_client.get_metrics()
                        await asyncio.sleep(1)
                    
                    if not hitl_action:
                        logger.info("HitL: Timeout reached without human intervention. Proceeding automatically.")
                    
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
        finally:
            await asyncio.to_thread(sandbox.stop)
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        logger.info(f"Pipeline completed. (Failed due to error: {e})")
        return False, [], llm_client.get_metrics()
    finally:
        if 'commit_sha' in locals() and commit_sha in processing_commits:
            processing_commits.remove(commit_sha)
