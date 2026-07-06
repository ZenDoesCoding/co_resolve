import logging
import subprocess

logger = logging.getLogger(__name__)

def check_docker_running() -> bool:
    """Checks if the Docker daemon is running and responding."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False

def ensure_docker_running(interactive: bool = False) -> bool:
    """Checks if Docker is running, and if not, attempts to load overlay and start it.
    If interactive is True, allows terminal password prompting (use for terminal entry points).
    If interactive is False, uses non-interactive sudo (use for background tasks).
    """
    if check_docker_running():
        logger.info("Docker daemon is already running and responding.")
        return True

    logger.warning("Docker daemon is not running. Attempting to start it...")

    sudo_cmd = ["sudo"] if interactive else ["sudo", "-n"]

    # 1. Attempt to load overlay kernel module if not loaded
    try:
        lsmod_res = subprocess.run(["lsmod"], capture_output=True, text=True)
        if "overlay" not in lsmod_res.stdout:
            logger.info("Overlay module not detected in lsmod. Running modprobe overlay...")
            subprocess.run(sudo_cmd + ["modprobe", "overlay"], check=True)
            logger.info("Successfully loaded overlay kernel module.")
    except Exception as e:
        logger.warning(f"Could not load overlay module (this might be fine if built-in): {e}")

    # 2. Attempt to start the docker service
    try:
        systemctl_check = subprocess.run(["which", "systemctl"], capture_output=True)
        if systemctl_check.returncode == 0:
            logger.info("Starting Docker service via systemctl...")
            subprocess.run(sudo_cmd + ["systemctl", "start", "docker"], check=True)
        else:
            logger.info("Starting Docker service via service...")
            subprocess.run(sudo_cmd + ["service", "docker", "start"], check=True)
    except Exception as e:
        logger.error(f"Failed to start Docker service: {e}")
        return False

    # 3. Final verification check
    if check_docker_running():
        logger.info("Docker daemon successfully started and responding.")
        return True
    else:
        logger.error("Docker service start command executed, but daemon is still not responding.")
        return False
