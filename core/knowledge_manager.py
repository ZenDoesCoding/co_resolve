import json
import os
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

KNOWLEDGE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_knowledge.json")
PENDING_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pending_knowledge.json")


class KnowledgeManager:
    """Manages the agent's persistent knowledge base (RAG).
    
    Read path:  Always available. Injects all entries into the system prompt
                so the LLM sees every known pattern without extra API calls.
    Write path: Only available in turbo mode. Candidates are written to a
                pending file and promoted to the main KB only after human approval.
    """

    def __init__(self):
        self._ensure_files()

    def _ensure_files(self):
        """Create pending files if they don't exist."""
        if not os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "w") as f:
                json.dump({"candidates": []}, f, indent=2)

    # ─── READ PATH ───────────────────────────────────────────────

    def load_knowledge(self) -> List[Dict]:
        """Load all approved knowledge entries."""
        if not os.path.exists(KNOWLEDGE_FILE):
            return []
        try:
            with open(KNOWLEDGE_FILE, "r") as f:
                data = json.load(f)
            return data.get("entries", [])
        except json.JSONDecodeError:
            logger.warning("Could not parse knowledge base. Starting empty.")
            return []

    def format_for_prompt(self) -> str:
        """Format knowledge entries into a compact string for the system prompt.
        
        Returns empty string if no entries exist.
        Format is designed to be token-efficient and easy for the LLM to parse:
            KB-1: [pattern] ...
                  [problem] ...
                  [solution] ...
        """
        entries = self.load_knowledge()
        if not entries:
            return ""

        lines = [
            "### Knowledge Base (Lessons from Past Fixes)",
            "Use these patterns to recognize and fix similar issues. Do NOT ignore them.\n"
        ]

        for i, entry in enumerate(entries, 1):
            lines.append(f"KB-{i}: [pattern] {entry['pattern']}")
            lines.append(f"       [problem] {entry['problem']}")
            lines.append(f"       [solution] {entry['solution']}")
            lines.append("")  # blank line between entries

        return "\n".join(lines)

    # ─── WRITE PATH (turbo mode only) ────────────────────────────

    def add_candidate(self, pattern: str, problem: str, solution: str,
                      source_repo: str = "", source_commit: str = "") -> str:
        """Append a knowledge candidate to the pending file.
        
        Called by the agent during a run via tool call. These candidates
        are NOT yet in the main knowledge base – they wait for human approval.
        """
        try:
            with open(PENDING_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            data = {"candidates": []}

        candidate = {
            "id": uuid.uuid4().hex[:8],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pattern": pattern,
            "problem": problem,
            "solution": solution,
            "language": "python",
            "source_repo": source_repo,
            "source_commit": source_commit
        }

        data["candidates"].append(candidate)
        with open(PENDING_FILE, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Knowledge candidate recorded: {pattern[:60]}...")
        return f"Recorded knowledge candidate (pending approval): '{pattern[:60]}...'"

    # ─── APPROVAL GATE ───────────────────────────────────────────

    def promote_candidates(self) -> List[Dict]:
        """Read and return all pending candidates for human review.
        
        Returns an empty list if there are no pending candidates.
        """
        try:
            with open(PENDING_FILE, "r") as f:
                data = json.load(f)
            return data.get("candidates", [])
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def commit_approved(self, approved_ids: Optional[List[str]] = None):
        """Move approved candidates from pending into the main knowledge base.
        
        If approved_ids is None, ALL pending candidates are approved.
        """
        pending = self.promote_candidates()
        if not pending:
            return

        # Filter to only approved entries (or all if no specific IDs given)
        if approved_ids is not None:
            to_commit = [c for c in pending if c["id"] in approved_ids]
        else:
            to_commit = pending

        if not to_commit:
            return

        # Load existing knowledge
        try:
            with open(KNOWLEDGE_FILE, "r") as f:
                kb_data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            kb_data = {"entries": []}

        # Append approved entries
        kb_data["entries"].extend(to_commit)
        with open(KNOWLEDGE_FILE, "w") as f:
            json.dump(kb_data, f, indent=2)

        logger.info(f"Committed {len(to_commit)} knowledge entries to the main knowledge base.")

        # Clean pending file
        self.discard_candidates()

    def discard_candidates(self):
        """Clear all pending candidates without committing them."""
        with open(PENDING_FILE, "w") as f:
            json.dump({"candidates": []}, f, indent=2)
        logger.info("Pending knowledge candidates discarded.")
