import logging
import time
from collections import deque
from openai import AsyncOpenAI
from utils.config import settings
from utils.config_manager import yaml_config

logger = logging.getLogger(__name__)

class LLMClient:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
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

        # Prefer Google AI Studio to avoid Vertex costs (BYOK)
        extra_body = {
            "provider": {
                "order": ["google_ai_studio"]
            }
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
                    self.total_output_tokens += self.total_output_tokens
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
