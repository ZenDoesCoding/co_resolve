import importlib
import logging
from utils.active_arc import get_active_arc

logger = logging.getLogger(__name__)

# Shared global states expected by TUI, webhook, or benchmark
hitl_action = None
processing_commits = set()

class DynamicLLMClientProxy:
    @property
    def _active_client(self):
        arc_num = get_active_arc()
        arc_map = {
            1: "basic_arc",
            2: "openai_arc",
            3: "google_arc"
        }
        arc_name = arc_map.get(arc_num, f"arc_{arc_num}")
        module_name = f"architectures.{arc_name}.core.orchestrator"
        module = importlib.import_module(module_name)
        return module.llm_client

    def get_metrics(self):
        return self._active_client.get_metrics()

    def reset_metrics(self):
        return self._active_client.reset_metrics()

    def __getattr__(self, name):
        return getattr(self._active_client, name)

# Instantiate the proxy so benchmark.py or other code can import and query it
llm_client = DynamicLLMClientProxy()

async def run_agent_pipeline(repo_url: str, commit_sha: str, branch: str, run_id: int):
    global hitl_action
    if commit_sha in processing_commits:
        logger.warning(f"Pipeline already running for commit {commit_sha}. Skipping.")
        return False, [], {}
    processing_commits.add(commit_sha)
    
    arc_num = get_active_arc()
    arc_map = {
        1: "basic_arc",
        2: "openai_arc",
        3: "google_arc"
    }
    arc_name = arc_map.get(arc_num, f"arc_{arc_num}")
    module_name = f"architectures.{arc_name}.core.orchestrator"
    logger.info(f"🚀 [ROUTER] Dynamic routing to {module_name} ({arc_name})")
    
    try:
        module = importlib.import_module(module_name)
        # Sync hitl_action and processing_commits globals to the target module if they exist
        if hasattr(module, 'hitl_action'):
            module.hitl_action = hitl_action
        if hasattr(module, 'processing_commits'):
            module.processing_commits = processing_commits

        # Run pipeline
        res = await module.run_agent_pipeline(repo_url, commit_sha, branch, run_id)
        return res
    except Exception as e:
        logger.error(f"❌ [ROUTER] Failed to execute pipeline in {module_name}: {e}", exc_info=True)
        return False, [], {}
    finally:
        if commit_sha in processing_commits:
            processing_commits.remove(commit_sha)
