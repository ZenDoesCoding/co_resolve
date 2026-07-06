import docker
import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class SandboxValidator:
    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self.client = None
        self.container = None
        self.image_name = "python:3.11-slim"

    def apply_patch(self, filepath: str, modified_content: str):
        """Applies a direct file modification since LLM might return full blocks."""
        # Write the file inside the container to avoid host permission errors when files are owned by root.
        import base64
        import json
        try:
            b64_content = base64.b64encode(modified_content.encode('utf-8')).decode('utf-8')
            py_cmd = f"import os, base64; path = os.path.join('/workspace', {json.dumps(filepath)}); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, 'wb').write(base64.b64decode('{b64_content}'))"
            self.container.exec_run(["python", "-c", py_cmd])
            
            # Restore host user ownership for all files in the bind mount
            uid = os.getuid()
            self.container.exec_run(f"chown -R {uid}:{uid} /workspace")
        except Exception as e:
            logger.error(f"Failed to apply patch in container for {filepath}: {e}")
            raise
        logger.info(f"Updated file in sandbox (via container): {filepath}")

    def _get_expected_volumes(self):
        """Returns the volume configuration the container SHOULD have."""
        vols = {
            self.work_dir: {"bind": "/workspace", "mode": "rw"},
        }
        semgrep_path = os.path.abspath("semgrep_rules")
        if os.path.isdir(semgrep_path):
            vols[semgrep_path] = {"bind": "/semgrep_rules", "mode": "ro"}
        return vols

    def _check_volumes(self, container) -> bool:
        """Verify all expected bind mounts exist and point to the correct host source paths."""
        expected = self._get_expected_volumes()
        actual_mounts = container.attrs.get("Mounts", [])
        actual_mounts_map = {m["Destination"]: m["Source"] for m in actual_mounts}

        for host_src, config in expected.items():
            bind_dest = config["bind"]
            if bind_dest not in actual_mounts_map:
                logger.warning(f"Container is missing volume mount for target: {bind_dest}. Recreating...")
                return False
            # Verify host source matches
            if os.path.abspath(actual_mounts_map[bind_dest]) != os.path.abspath(host_src):
                logger.warning(f"Container volume mount {bind_dest} points to stale source: {actual_mounts_map[bind_dest]} instead of {host_src}. Recreating...")
                return False
        return True

    def start(self):
        """Starts or reuses a persistent Docker container."""
        from utils.docker_helper import ensure_docker_running
        if not ensure_docker_running(interactive=False):
            raise RuntimeError("Docker daemon is not running and could not be started non-interactively. Please start Docker manually or run the application/benchmark again.")
        
        self.client = docker.from_env()
        container_name = "co_resolve_sandbox"
        try:
            self.container = self.client.containers.get(container_name)

            # Validate that all required volumes are mounted
            if not self._check_volumes(self.container):
                logger.info("Destroying stale container to apply new volume mounts...")
                self.container.remove(force=True)
                raise docker.errors.NotFound("Forced recreation for volume update")

            if self.container.status != "running":
                logger.info("Starting existing persistent sandbox container...")
                self.container.start()
            else:
                logger.info("Reusing running persistent sandbox container...")
                self.container.restart()
            return
        except docker.errors.NotFound:
            logger.info("Starting new persistent sandbox container...")
            self.container = self.client.containers.run(
                self.image_name,
                command="tail -f /dev/null",
                name=container_name,
                volumes=self._get_expected_volumes(),
                working_dir="/workspace",
                detach=True,
                remove=False,
                network_mode="host"
            )
            
            # Pre-install dependencies ONLY on creation
            logger.info("Pre-installing dependencies in sandbox...")
            setup_cmd = "pip install pytest --quiet --no-warn-script-location"
            res = self.container.exec_run(f"bash -c '{setup_cmd}'")
            logger.info(f"Sandbox setup output: {res.output.decode('utf-8').strip()}")

    def execute_test(self, test_command: str) -> Dict[str, Any]:
        """Runs the command in the persistent container."""
        logger.info(f"Running test in persistent sandbox: {test_command}")
        if not self.container:
            return {"exit_code": 1, "logs": "Error: Sandbox not started."}
        res = self.container.exec_run(["bash", "-c", test_command])
        
        # Restore host user ownership for any files created/modified in the bind mount
        try:
            uid = os.getuid()
            self.container.exec_run(f"chown -R {uid}:{uid} /workspace")
        except Exception as e:
            logger.warning(f"Failed to restore file ownership in sandbox after test: {e}")

        return {
            "exit_code": res.exit_code,
            "logs": res.output.decode('utf-8')
        }

    def stop(self):
        """Do nothing to keep the container alive for reuse."""
        logger.info("Keeping persistent sandbox container alive.")
        self.container = None
