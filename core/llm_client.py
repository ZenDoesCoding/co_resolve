import logging
import time
import json
from collections import deque
from openai import AsyncOpenAI
from utils.config import settings
from utils.config_manager import yaml_config
from google import genai

logger = logging.getLogger(__name__)

class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

class StreamingResponseWrapper:
    def __init__(self, generator):
        self.generator = generator
        self.steps = []
        self.usage = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.generator.__anext__()

def convert_tools_to_native(tools):
    if not tools:
        return None
    native_tools = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function" and "function" in tool:
            fn = tool["function"]
            native_tools.append({
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description"),
                "parameters": fn.get("parameters")
            })
        else:
            native_tools.append(tool)
    return native_tools

def openai_messages_to_steps(messages):
    steps = []
    system_instruction = None
    tool_call_id_to_name = {}
    
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            system_instruction = content
        elif role == "user":
            steps.append({
                "type": "user_input",
                "content": [{"type": "text", "text": content}]
            })
        elif role == "assistant":
            before_len = len(steps)
            has_action_or_output = False
            if "steps" in msg and msg["steps"]:
                steps.extend(msg["steps"])
                for s in msg["steps"]:
                    if isinstance(s, dict):
                        stype = s.get("type")
                        if stype in ("model_output", "function_call"):
                            has_action_or_output = True
                        if stype == "function_call":
                            tc_id = s.get("id")
                            name = s.get("name")
                            if tc_id and name:
                                tool_call_id_to_name[tc_id] = name
            else:
                if content:
                    steps.append({
                        "type": "model_output",
                        "content": [{"type": "text", "text": content}]
                    })
                    has_action_or_output = True
                if msg.get("tool_calls"):
                    has_action_or_output = True
                    for tc in msg["tool_calls"]:
                        if isinstance(tc, dict):
                            args_str = tc.get("function", {}).get("arguments", "{}")
                            name = tc.get("function", {}).get("name", "")
                            tc_id = tc.get("id")
                            signature = tc.get("signature") or tc.get("thought_signature")
                        else:
                            args_str = getattr(tc.function, "arguments", "{}")
                            name = getattr(tc.function, "name", "")
                            tc_id = getattr(tc, "id", None)
                            signature = getattr(tc, "signature", None) or getattr(tc, "thought_signature", None)
                        
                        if tc_id and name:
                            tool_call_id_to_name[tc_id] = name
                            
                        try:
                            arguments_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except Exception:
                            arguments_dict = {}
                        
                        if signature:
                            steps.append({
                                "type": "thought",
                                "signature": signature,
                                "summary": None
                            })
                        steps.append({
                            "type": "function_call",
                            "id": tc_id,
                            "name": name,
                            "arguments": arguments_dict
                        })
            if not has_action_or_output:
                steps.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": "I am continuing my analysis."}]
                })
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            name = msg.get("name") or tool_call_id_to_name.get(call_id)
            if not name:
                logger.warning(f"Could not resolve function name for tool call ID: {call_id}. Using empty string fallback.")
                name = ""
            steps.append({
                "type": "function_result",
                "call_id": call_id,
                "name": name,
                "result": [{"type": "text", "text": content}]
            })
    return steps, system_instruction

async def google_stream_generator(stream, wrapper):
    steps = []
    tool_call_index_map = {}
    
    async for event in stream:
        logger.debug(f"[STREAM_EVENT] {event.event_type} - {event}")
        if event.event_type == "error":
            error_msg = getattr(event.error, 'message', '') or str(event.error)
            error_code = getattr(event.error, 'code', 'unknown')
            raise Exception(f"Interactions API error: {error_code} - {error_msg}")
            
        if event.event_type == "step.start":
            idx = event.index
            step_data = event.step
            while len(steps) <= idx:
                steps.append(None)
            
            if step_data.type == "thought":
                steps[idx] = {
                    "type": "thought",
                    "signature": getattr(step_data, 'signature', None),
                    "summary": None
                }
            elif step_data.type == "function_call":
                # step.start gives us name and id but arguments are empty;
                # the real arguments arrive later in a step.delta with
                # type="arguments_delta".
                steps[idx] = {
                    "type": "function_call",
                    "id": getattr(step_data, 'id', None),
                    "name": getattr(step_data, 'name', None),
                    "arguments": getattr(step_data, 'arguments', {}) or {}
                }
                if idx not in tool_call_index_map:
                    tool_call_index_map[idx] = len(tool_call_index_map)
                tc_idx = tool_call_index_map[idx]
                
                sig = None
                if idx > 0 and steps[idx-1] and steps[idx-1].get("type") == "thought":
                    sig = steps[idx-1].get("signature")
                
                # Yield the initial tool_call chunk with name but empty args.
                # The orchestrator accumulates these; the arguments chunk
                # follows when we handle arguments_delta below.
                yield AttrDict({
                    "choices": [
                        AttrDict({
                            "delta": AttrDict({
                                "content": None,
                                "reasoning_content": None,
                                "tool_calls": [
                                    AttrDict({
                                        "index": tc_idx,
                                        "id": step_data.id,
                                        "type": "function",
                                        "function": AttrDict({
                                            "name": step_data.name,
                                            "arguments": ""
                                        }),
                                        "model_extra": {
                                            "thought_signature": sig
                                        } if sig else {}
                                    })
                                ]
                            })
                        })
                    ]
                })
            elif step_data.type == "model_output":
                steps[idx] = {
                    "type": "model_output",
                    "content": []
                }
                
        elif event.event_type == "step.delta":
            idx = event.index
            delta = event.delta
            while len(steps) <= idx:
                steps.append(None)
            if steps[idx] is None:
                steps[idx] = {}
                
            if delta.type == "thought_signature":
                steps[idx]["type"] = "thought"
                steps[idx]["signature"] = getattr(delta, 'signature', None)
            elif delta.type == "thought_summary":
                steps[idx]["type"] = "thought"
                summary_part = getattr(delta, 'content', None)
                if summary_part:
                    if steps[idx].get("summary") is None:
                        steps[idx]["summary"] = []
                    
                    if hasattr(summary_part, 'model_dump'):
                        steps[idx]["summary"].append(summary_part.model_dump())
                    elif isinstance(summary_part, dict):
                        steps[idx]["summary"].append(summary_part)
                    else:
                        steps[idx]["summary"].append({"type": "text", "text": getattr(summary_part, 'text', '')})
                    
                    text = getattr(summary_part, 'text', '') if not isinstance(summary_part, dict) else summary_part.get('text', '')
                    if text:
                        yield AttrDict({
                            "choices": [
                                AttrDict({
                                    "delta": AttrDict({
                                        "content": None,
                                        "reasoning_content": text,
                                        "tool_calls": None
                                    })
                                })
                            ]
                        })
            elif delta.type == "arguments_delta":
                # This is where the actual function call arguments arrive
                # during streaming. The arguments field is a JSON string.
                args_json = getattr(delta, 'arguments', None) or "{}"
                try:
                    args_dict = json.loads(args_json) if isinstance(args_json, str) else args_json
                except (json.JSONDecodeError, TypeError):
                    args_dict = {}
                
                # Update the stored step with the real arguments
                if steps[idx] and steps[idx].get("type") == "function_call":
                    steps[idx]["arguments"] = args_dict
                
                # Yield a streaming chunk with the arguments so the
                # orchestrator can accumulate them into the tool_call object.
                tc_idx = tool_call_index_map.get(idx, 0)
                yield AttrDict({
                    "choices": [
                        AttrDict({
                            "delta": AttrDict({
                                "content": None,
                                "reasoning_content": None,
                                "tool_calls": [
                                    AttrDict({
                                        "index": tc_idx,
                                        "id": None,
                                        "type": None,
                                        "function": AttrDict({
                                            "name": None,
                                            "arguments": args_json
                                        })
                                    })
                                ]
                            })
                        })
                    ]
                })
            elif delta.type == "text":
                steps[idx]["type"] = "model_output"
                if "content" not in steps[idx]:
                    steps[idx]["content"] = []
                steps[idx]["content"].append({"type": "text", "text": delta.text})
                
                yield AttrDict({
                    "choices": [
                        AttrDict({
                            "delta": AttrDict({
                                "content": delta.text,
                                "reasoning_content": None,
                                "tool_calls": None
                            })
                        })
                    ]
                })
                
        elif event.event_type == "interaction.completed":
            wrapper.usage = event.interaction.usage
            # Yield final chunk with usage
            yield AttrDict({
                "choices": [
                    AttrDict({
                        "delta": AttrDict({
                            "content": None,
                            "reasoning_content": None,
                            "tool_calls": None
                        })
                    })
                ],
                "usage": AttrDict({
                    "prompt_tokens": event.interaction.usage.total_input_tokens,
                    "completion_tokens": event.interaction.usage.total_output_tokens
                })
            })
            
    wrapper.steps = [s for s in steps if s is not None]


class LLMClient:
    def __init__(self):
        if settings.llm_provider == "google":
            api_key = settings.gemini_api_key or settings.openai_api_key
            self.google_client = genai.Client(api_key=api_key)
            self.client = None
            logger.info("Using Google GenAI native Interactions Client.")
        elif settings.openai_base_url and "generativelanguage.googleapis.com" in settings.openai_base_url:
            self.client = AsyncOpenAI(
                api_key=settings.gemini_api_key or settings.openai_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            self.google_client = None
            logger.info("Using Google AI Studio direct endpoint.")
        else:
            self.client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
            self.google_client = None
        self._call_timestamps = deque()
        self._day_call_timestamps = deque()
        self._tpm_tracker = deque()
        self.api_invocations = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.is_estimated = False

    def get_metrics(self):
        return {
            "api_invocations": self.api_invocations,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "last_input_tokens": self.last_input_tokens,
            "last_output_tokens": self.last_output_tokens,
            "is_estimated": self.is_estimated
        }

    def reset_metrics(self):
        self.api_invocations = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.is_estimated = False

    def _record_token_usage(self):
        self._tpm_tracker.append((time.time(), self.last_input_tokens + self.last_output_tokens))

    async def generate(self, messages, tools=None, stream=False):
        # Rate limiting logic
        max_rpm = yaml_config["agent"].get("max_rpm", 15)
        max_tpm = yaml_config["agent"].get("max_tpm", 250000)
        max_rpd = yaml_config["agent"].get("max_rpd", 500)
        
        now = time.time()
        import asyncio
        
        # 1. Requests Per Day (RPD)
        while self._day_call_timestamps and self._day_call_timestamps[0] < now - 86400:
            self._day_call_timestamps.popleft()
        if len(self._day_call_timestamps) >= max_rpd:
            sleep_time = 86400 - (now - self._day_call_timestamps[0])
            if sleep_time > 0:
                logger.info(f"Daily request limit (RPD) reached. Sleeping for {sleep_time:.2f} seconds...")
                await asyncio.sleep(sleep_time)
                now = time.time()

        # 2. Tokens Per Minute (TPM)
        while self._tpm_tracker and self._tpm_tracker[0][0] < now - 60:
            self._tpm_tracker.popleft()
        current_tpm = sum(entry[1] for entry in self._tpm_tracker)
        if current_tpm >= max_tpm:
            temp_sum = current_tpm
            for timestamp, tokens in self._tpm_tracker:
                if temp_sum < max_tpm:
                    break
                sleep_time = 60 - (time.time() - timestamp)
                if sleep_time > 0:
                    logger.info(f"Token rate limit (TPM) reached. Sleeping for {sleep_time:.2f} seconds...")
                    await asyncio.sleep(sleep_time)
                temp_sum -= tokens
            now = time.time()

        # 3. Requests Per Minute (RPM)
        while self._call_timestamps and self._call_timestamps[0] < now - 60:
            self._call_timestamps.popleft()
        if len(self._call_timestamps) >= max_rpm:
            sleep_time = 60 - (now - self._call_timestamps[0])
            if sleep_time > 0:
                logger.info(f"Rate limit reached. Sleeping for {sleep_time:.2f} seconds...")
                await asyncio.sleep(sleep_time)

        # Record call timestamps
        call_time = time.time()
        self._call_timestamps.append(call_time)
        self._day_call_timestamps.append(call_time)
        self.api_invocations += 1

        # Estimate tokens proactively (1 token per 4 chars of message, plus a 1000 token buffer for output)
        est_tokens = len(str(messages)) // 4 + 1000
        self._tpm_tracker.append((time.time(), est_tokens))

        if settings.llm_provider == "google":
            has_user_message = any(msg.get("role") == "user" for msg in messages)
            if not has_user_message:
                messages = list(messages)
                system_idx = -1
                for idx, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        system_idx = idx
                user_msg = {"role": "user", "content": "Please start your analysis and use tools if needed."}
                if system_idx != -1:
                    messages.insert(system_idx + 1, user_msg)
                else:
                    messages.insert(0, user_msg)

            steps, system_instruction = openai_messages_to_steps(messages)
            native_tools = convert_tools_to_native(tools)
            
            try:
                if stream:
                    stream_obj = await self.google_client.aio.interactions.create(
                        model=settings.llm_model,
                        input=steps,
                        system_instruction=system_instruction,
                        tools=native_tools,
                        generation_config={
                            "thinking_summaries": "auto",
                            "thinking_level": settings.thinking_level.lower() if settings.thinking_level else "high"
                        },
                        stream=True
                    )
                    wrapper = StreamingResponseWrapper(None)
                    
                    async def tracking_generator():
                        usage_recorded = False
                        try:
                            async for chunk in google_stream_generator(stream_obj, wrapper):
                                yield chunk
                            if wrapper.usage:
                                self.last_input_tokens = getattr(wrapper.usage, 'total_input_tokens', 0) or 0
                                self.last_output_tokens = getattr(wrapper.usage, 'total_output_tokens', 0) or 0
                                self.total_input_tokens += self.last_input_tokens
                                self.total_output_tokens += self.last_output_tokens
                                logger.info(f"[TOKEN_USAGE] {self.last_input_tokens} {self.last_output_tokens}")
                                diff = (self.last_input_tokens + self.last_output_tokens) - est_tokens
                                self._tpm_tracker.append((time.time(), diff))
                                usage_recorded = True
                        except Exception:
                            if not usage_recorded:
                                self._tpm_tracker.append((time.time(), -est_tokens))
                                usage_recorded = True
                            raise
                        finally:
                            if not usage_recorded:
                                self._tpm_tracker.append((time.time(), -est_tokens))
                            # Set wrapper steps at the end
                            wrapper.steps = getattr(wrapper, 'steps', [])
                        
                    wrapper.generator = tracking_generator()
                    return wrapper
                else:
                    interaction = await self.google_client.aio.interactions.create(
                        model=settings.llm_model,
                        input=steps,
                        system_instruction=system_instruction,
                        tools=native_tools,
                        generation_config={
                            "thinking_summaries": "auto",
                            "thinking_level": settings.thinking_level.lower() if settings.thinking_level else "high"
                        }
                    )
                    
                    # Parse steps
                    content = ""
                    tool_calls = []
                    reasoning_content = ""
                    interaction_steps = []
                    tool_call_index_map = {}
                    
                    for step in interaction.steps:
                        step_dict = {"type": step.type}
                        if step.type == "thought":
                            step_dict["signature"] = getattr(step, 'signature', None)
                            step_dict["summary"] = None
                            if getattr(step, 'summary', None):
                                summary_parts = []
                                for part in step.summary:
                                    if hasattr(part, 'model_dump'):
                                        summary_parts.append(part.model_dump())
                                    elif isinstance(part, dict):
                                        summary_parts.append(part)
                                    else:
                                        summary_parts.append({"type": "text", "text": getattr(part, 'text', '')})
                                    
                                    text = getattr(part, 'text', '') if not isinstance(part, dict) else part.get('text', '')
                                    if text:
                                        reasoning_content += text
                                step_dict["summary"] = summary_parts
                        elif step.type == "function_call":
                            step_dict["id"] = getattr(step, 'id', None)
                            step_dict["name"] = getattr(step, 'name', None)
                            step_dict["arguments"] = getattr(step, 'arguments', {}) or {}
                            
                            tc_id = getattr(step, 'id', None)
                            tc_name = getattr(step, 'name', None)
                            tc_args = getattr(step, 'arguments', {}) or {}
                            
                            sig = None
                            if len(interaction_steps) > 0 and interaction_steps[-1]["type"] == "thought":
                                sig = interaction_steps[-1].get("signature")
                                
                            tc_idx = len(tool_call_index_map)
                            tool_call_index_map[tc_idx] = tc_idx
                            
                            tool_calls.append(AttrDict({
                                "id": tc_id,
                                "type": "function",
                                "function": AttrDict({
                                    "name": tc_name,
                                    "arguments": json.dumps(tc_args) if isinstance(tc_args, dict) else tc_args
                                }),
                                "model_extra": {
                                    "thought_signature": sig
                                } if sig else {}
                            }))
                        elif step.type == "model_output":
                            step_content = []
                            if getattr(step, 'content', None):
                                for part in step.content:
                                    if getattr(part, 'text', None):
                                        content += part.text
                                        step_content.append({"type": "text", "text": part.text})
                            step_dict["content"] = step_content
                        
                        interaction_steps.append(step_dict)
                        
                    mock_choice = AttrDict({
                        "message": AttrDict({
                            "role": "assistant",
                            "content": content if content else None,
                            "tool_calls": tool_calls if tool_calls else None,
                            "reasoning_content": reasoning_content if reasoning_content else None,
                            "steps": interaction_steps
                        })
                    })
                    
                    mock_response = AttrDict({
                        "choices": [mock_choice],
                        "steps": interaction_steps,
                        "usage": AttrDict({
                            "prompt_tokens": interaction.usage.total_input_tokens if interaction.usage else 0,
                            "completion_tokens": interaction.usage.total_output_tokens if interaction.usage else 0
                        }) if interaction.usage else None
                    })
                    
                    if interaction.usage:
                        self.last_input_tokens = interaction.usage.total_input_tokens
                        self.last_output_tokens = interaction.usage.total_output_tokens
                        self.total_input_tokens += self.last_input_tokens
                        self.total_output_tokens += self.last_output_tokens
                    else:
                        self.is_estimated = True
                        self.last_input_tokens = len(str(messages)) // 4
                        self.last_output_tokens = len(content) // 4
                        self.total_input_tokens += self.last_input_tokens
                        self.total_output_tokens += self.last_output_tokens
                        
                    logger.info(f"[TOKEN_USAGE] {self.last_input_tokens} {self.last_output_tokens}")
                    diff = (self.last_input_tokens + self.last_output_tokens) - est_tokens
                    self._tpm_tracker.append((time.time(), diff))
                    return mock_response
            except Exception as e:
                self._tpm_tracker.append((time.time(), -est_tokens))
                logger.error(f"LLM call failed: {e}")
                raise

        # Non-google client logic (OpenAI)
        extra_body = {}
        if settings.openai_base_url and "openrouter.ai" in settings.openai_base_url:
            provider = settings.llm_provider
            if provider == "OpenInference":
                provider = "open-inference"
            extra_body["provider"] = {
                "only": [provider],
                "allow_fallbacks": False
            }
        
        # Enable reasoning for Gemma 4 models on OpenRouter
        if yaml_config["agent"].get("reasoning_mode", True):
            if "gemma-4" in settings.llm_model:
                extra_body["reasoning"] = {"enabled": True}

        if stream:
            extra_body["stream_options"] = {"include_usage": True}

        try:
            response = await self.client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                tools=tools,
                extra_body=extra_body,
                stream=stream
            )
            
            if not stream:
                if hasattr(response, 'usage') and response.usage:
                    self.last_input_tokens = response.usage.prompt_tokens
                    self.last_output_tokens = response.usage.completion_tokens
                    self.total_input_tokens += self.last_input_tokens
                    self.total_output_tokens += self.last_output_tokens
                else:
                    self.is_estimated = True
                    self.last_input_tokens = len(str(messages)) // 4
                    self.last_output_tokens = len(str(response.choices[0].message.content or "")) // 4 if hasattr(response, 'choices') and response.choices else 0
                    self.total_input_tokens += self.last_input_tokens
                    self.total_output_tokens += self.last_output_tokens
                logger.info(f"[TOKEN_USAGE] {self.last_input_tokens} {self.last_output_tokens}")
                diff = (self.last_input_tokens + self.last_output_tokens) - est_tokens
                self._tpm_tracker.append((time.time(), diff))
                return response
            else:
                async def tracking_generator():
                    full_output = ""
                    usage_found = False
                    try:
                        async for chunk in response:
                            if hasattr(chunk, 'choices') and chunk.choices and hasattr(chunk.choices[0], 'delta') and chunk.choices[0].delta.content:
                                full_output += chunk.choices[0].delta.content
                            
                            # Usage might be in the last chunk
                            if hasattr(chunk, 'usage') and chunk.usage:
                                self.last_input_tokens = chunk.usage.prompt_tokens
                                self.last_output_tokens = chunk.usage.completion_tokens
                                self.total_input_tokens += self.last_input_tokens
                                self.total_output_tokens += self.last_output_tokens
                                usage_found = True
                                logger.info(f"[TOKEN_USAGE] {self.last_input_tokens} {self.last_output_tokens}")
                                diff = (self.last_input_tokens + self.last_output_tokens) - est_tokens
                                self._tpm_tracker.append((time.time(), diff))
                                
                            yield chunk
                            
                        if not usage_found:
                            self.is_estimated = True
                            self.last_input_tokens = len(str(messages)) // 4
                            self.last_output_tokens = len(full_output) // 4
                            self.total_input_tokens += self.last_input_tokens
                            self.total_output_tokens += self.last_output_tokens
                            logger.info(f"[TOKEN_USAGE] {self.last_input_tokens} {self.last_output_tokens}")
                            diff = (self.last_input_tokens + self.last_output_tokens) - est_tokens
                            self._tpm_tracker.append((time.time(), diff))
                            usage_found = True
                    except Exception:
                        if not usage_found:
                            self._tpm_tracker.append((time.time(), -est_tokens))
                            usage_found = True
                        raise
                    finally:
                        if not usage_found:
                            self._tpm_tracker.append((time.time(), -est_tokens))
                            
                return tracking_generator()
        except Exception as e:
            self._tpm_tracker.append((time.time(), -est_tokens))
            logger.error(f"LLM call failed: {e}")
            raise
