import os
import yaml
import logging

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.yaml"

DEFAULT_CONFIG = {
    "agent": {
        "turbo_mode": False,
        "reasoning_mode": True,
        "max_attempts": 30,
        "max_rpm": 15,
        "max_tpm": 250000,
        "max_rpd": 500
    },
    "tui": {
        "show_server_logs": False,
        "theme": "textual-dark",
        "debug_mode": True
    }
}

COMMENTS = """# Co-Resolve Configuration File
# -----------------------------
# agent.turbo_mode (bool): 
#   If True, automatically pushes fixes and creates a PR without waiting.
#   Default: False
#
# agent.reasoning_mode (bool):
#   If True, enables advanced reasoning for supported models (e.g. gemma-4).
#   Default: True
#
# agent.max_attempts (int):
#   Maximum number of tool calls the agent can make before giving up.
#   Range: 1 to 100
#   Default: 30
#
# agent.max_rpm (int):
#   Maximum requests per minute for the LLM.
#   Default: 15
#
# agent.max_tpm (int):
#   Maximum tokens per minute across all LLM calls.
#   Default: 250000
#
# agent.max_rpd (int):
#   Maximum requests per day for the LLM.
#   Default: 500
#
# tui.show_server_logs (bool):
#   If True, raw web server logs (e.g. "Webhook received") are shown in the TUI History.
#   Default: False
#
# tui.theme (string):
#   The theme to use for the TUI.
#   Options: textual-dark, textual-light (or others if registered)
#   Default: textual-dark
#
# tui.debug_mode (bool):
#   If True, shows all logs in the Chat Window (like before the TUI).
#   If False, only shows high-level agent messages.
#   Default: True
"""

class ConfigManager:
    @staticmethod
    def ensure_config_exists():
        if not os.path.exists(CONFIG_FILE):
            logger.info(f"Creating default {CONFIG_FILE}")
            with open(CONFIG_FILE, "w") as f:
                f.write(COMMENTS + "\n")
                yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)

    @staticmethod
    def load_config():
        ConfigManager.ensure_config_exists()
        try:
            with open(CONFIG_FILE, "r") as f:
                config = yaml.safe_load(f)
                if not config:
                    return DEFAULT_CONFIG
                # Deep merge defaults in case of missing keys
                merged = DEFAULT_CONFIG.copy()
                for section in DEFAULT_CONFIG:
                    if section in config and isinstance(config[section], dict):
                        merged[section].update(config[section])
                return merged
        except Exception as e:
            logger.error(f"Error loading {CONFIG_FILE}: {e}")
            return DEFAULT_CONFIG

    @staticmethod
    def save_config(config):
        try:
            with open(CONFIG_FILE, "w") as f:
                f.write(COMMENTS + "\n")
                yaml.dump(config, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Error saving {CONFIG_FILE}: {e}")

yaml_config = ConfigManager.load_config()
