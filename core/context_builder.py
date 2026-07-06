import os
import re
import subprocess
import tempfile
import tree_sitter
import tree_sitter_python
import logging
from typing import List, Dict
from utils.config import settings

logger = logging.getLogger(__name__)

class RepoMapGenerator:
    def __init__(self, repo_url: str, commit_sha: str = None):
        self.repo_url = repo_url
        self.commit_sha = commit_sha
        self.work_dir = os.path.abspath("workspace")
        
        # Setup Tree-sitter for Multi-Language (starting with Python)
        self.LANGUAGE = tree_sitter.Language(tree_sitter_python.language())
        self.parser = tree_sitter.Parser(self.LANGUAGE)

    def clone_repo(self):
        """Clones the repository and checks out the specific commit."""
        repo_url = self.repo_url
        if settings.github_token and "github.com" in repo_url:
            repo_url = repo_url.replace("https://github.com/", f"https://{settings.github_token}@github.com/")
            
        import os
        import subprocess
        
        # Configure non-interactive git environment to prevent hanging on auth prompts
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        
        if not os.path.exists(self.work_dir):
            os.makedirs(self.work_dir, exist_ok=True)
            
        try:
            # Initialize if not already a git repo
            if not os.path.exists(os.path.join(self.work_dir, ".git")):
                subprocess.run(["git", "init"], cwd=self.work_dir, check=True, capture_output=True, env=env)
                subprocess.run(["git", "remote", "add", "origin", repo_url], cwd=self.work_dir, check=True, capture_output=True, env=env)
            else:
                subprocess.run(["git", "remote", "set-url", "origin", repo_url], cwd=self.work_dir, check=True, capture_output=True, env=env)
                    
            # Clean directory contents using helper container to handle root files, but keep .git!
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.work_dir}:/workspace", "alpine", "sh", "-c", "find /workspace -mindepth 1 -not -path '/workspace/.git*' -delete"], check=True)
            
            # Fetch and reset
            ref = self.commit_sha if self.commit_sha else self.branch
            logger.info(f"Fetching ref `{ref}` from `{repo_url}`...")
            
            subprocess.run(["git", "fetch", "origin", ref], cwd=self.work_dir, check=True, capture_output=True, text=True, env=env)
            subprocess.run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=self.work_dir, check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            cmd_str = " ".join(e.cmd)
            stderr_output = e.stderr.strip() if e.stderr else "No stderr output"
            logger.error(f"Git command failed: `{cmd_str}`\nStderr: {stderr_output}")
            raise

    def generate_map(self) -> str:
        """Walks the repository and generates a lightweight map of classes/functions."""
        repo_map_lines = []
        for root, _, files in os.walk(self.work_dir):
            if '.git' in root:
                continue
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, self.work_dir)
                    signatures = self._extract_signatures(file_path)
                    if signatures:
                        repo_map_lines.append(f"File: {rel_path}")
                        repo_map_lines.extend([f"  {sig}" for sig in signatures])
                        repo_map_lines.append("")
        return "\n".join(repo_map_lines)

    def _extract_signatures(self, file_path: str) -> List[str]:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            code = f.read()
        
        tree = self.parser.parse(bytes(code, "utf8"))
        signatures = []
        
        def walk(node, depth=0):
            if node.type == 'class_definition':
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = code[name_node.start_byte:name_node.end_byte]
                    signatures.append(f"{'  '*depth}class {name}:")
            elif node.type == 'function_definition':
                name_node = node.child_by_field_name('name')
                params_node = node.child_by_field_name('parameters')
                if name_node:
                    name = code[name_node.start_byte:name_node.end_byte]
                    params = code[params_node.start_byte:params_node.end_byte] if params_node else "()"
                    signatures.append(f"{'  '*depth}def {name}{params}:")
                    
            for child in node.children:
                walk(child, depth + 1 if node.type in ('class_definition', 'function_definition') else depth)
                
        walk(tree.root_node)
        return signatures

    def get_predictive_context(self, logs: str) -> str:
        """Parses the failure logs to find file paths and line numbers, and returns code snippets."""
        # Look for standard Python traceback format: File "path/to/file.py", line XYZ, in ...
        pattern = re.compile(r'File "([^"]+\.py)", line (\d+)')
        matches = pattern.findall(logs)
        
        if not matches:
            return ""
            
        context_lines = ["### Predictive Context (Auto-Extracted from Traceback)"]
        added_files = set()
        
        for file_path, line_str in matches:
            line_num = int(line_str)
            # Make sure it's a relative path in our repo
            full_path = file_path
            if not os.path.isabs(full_path):
                full_path = os.path.join(self.work_dir, file_path)
            
            # Normalize to relative path
            try:
                rel_path = os.path.relpath(full_path, self.work_dir)
            except ValueError:
                continue
                
            # Avoid duplicating the same file heavily, or limit to first few lines of context per file
            if rel_path in added_files:
                continue
                
            if not os.path.exists(full_path):
                continue
                
            added_files.add(rel_path)
            
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
            start_line = max(0, line_num - 15)
            end_line = min(len(lines), line_num + 15)
            
            snippet = "".join(lines[start_line:end_line])
            context_lines.append(f"\nFile: `{rel_path}` (Lines {start_line+1}-{end_line}):\n```python\n{snippet}```")
            
        return "\n".join(context_lines)

