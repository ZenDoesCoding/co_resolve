from github import Github, InputGitTreeElement
from utils.config import settings
from utils.config_manager import yaml_config
import logging
from typing import Dict

logger = logging.getLogger(__name__)

class PRService:
    def __init__(self):
        # Use PAT auth for simplicity if App ID is not fully set up
        self.github = Github(settings.github_token)

    def push_fix(self, repo_url: str, branch: str, files_to_update: Dict[str, str], rca_markdown: str, commit_sha: str):
        """
        Pushes the fix either via direct commit (Turbo mode) or via a new Pull Request.
        """
        try:
            # Assuming URL like https://github.com/owner/repo.git
            repo_name = repo_url.split("github.com/")[-1].replace(".git", "")
            repo = self.github.get_repo(repo_name)
        except Exception as e:
            logger.error(f"Failed to access repo {repo_url}: {e}")
            return

        turbo_mode = yaml_config["agent"]["turbo_mode"]
        if (branch == "main" or branch == "master") and turbo_mode:
            logger.warning("Turbo mode requested but pushing directly to main/master is forbidden! Falling back to PR mode.")
            turbo_mode = False

        if turbo_mode:
            logger.info("Turbo mode enabled. Committing directly to branch.")
            commit_message = f"fix: resolve CI failure on {commit_sha}\n\n{rca_markdown}"
            self._commit_to_branch(repo, branch, files_to_update, commit_message)
        else:
            import uuid
            logger.info("Turbo mode disabled. Creating a new branch and PR.")
            run_suffix = uuid.uuid4().hex[:4]
            new_branch_name = f"fix/agent-resolution-{commit_sha[:7]}-{run_suffix}"
            self._create_branch(repo, branch, new_branch_name)
            
            commit_message = f"fix: resolve CI failure on {commit_sha}"
            self._commit_to_branch(repo, new_branch_name, files_to_update, commit_message)
            
            pr_title = f"Auto-fix for failed commit {commit_sha[:7]}"
            self._create_pull_request(repo, new_branch_name, branch, pr_title, rca_markdown)

    def _commit_to_branch(self, repo, branch: str, files_to_update: Dict[str, str], commit_message: str):
        ref = repo.get_git_ref(f"heads/{branch}")
        latest_commit = repo.get_commit(ref.object.sha)
        base_tree = repo.get_git_tree(latest_commit.sha, recursive=False)

        elements = []
        for file_path, content in files_to_update.items():
            blob = repo.create_git_blob(content, "utf-8")
            elements.append(
                InputGitTreeElement(path=file_path, mode='100644', type='blob', sha=blob.sha)
            )

        new_tree = repo.create_git_tree(elements, base_tree)
        new_commit = repo.create_git_commit(commit_message, new_tree, [repo.get_git_commit(latest_commit.sha)])
        ref.edit(new_commit.sha)

    def _create_branch(self, repo, source_branch: str, new_branch: str):
        ref = repo.get_git_ref(f"heads/{source_branch}")
        try:
            repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=ref.object.sha)
        except Exception as e:
            logger.warning(f"Branch might already exist or creation failed: {e}")

    def _create_pull_request(self, repo, head_branch: str, base_branch: str, title: str, body: str):
        try:
            pr = repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
            logger.info(f"Created Pull Request: {pr.html_url}")
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
