"""
tools.py
--------
Every tool the agent is allowed to call, all sandboxed to the target
repository's root directory. No tool can read/write/execute anything
outside REPO_ROOT (path-traversal is blocked explicitly).

Each function returns a plain string (or a JSON-serializable dict) that
gets fed back to the model as the tool_result content.
"""

import os
import re
import subprocess
import fnmatch
from pathlib import Path

# Directories we never want to walk into when exploring the repo
IGNORE_DIRS = {".git", "node_modules", "dist", "build", "coverage", ".idea", ".vscode"}

# Whitelisted read-only git subcommands. Nothing that mutates history/remote.
ALLOWED_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch"}


class RepoTools:
    """All tools are bound to a single repo root so paths can be validated."""

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.exists():
            raise FileNotFoundError(f"Repo root does not exist: {repo_root}")

    # ---------- internal helpers ----------

    def _resolve(self, rel_path: str) -> Path:
        """Resolve a path relative to repo_root and refuse to escape it."""
        candidate = (self.repo_root / rel_path).resolve()
        if self.repo_root not in candidate.parents and candidate != self.repo_root:
            raise PermissionError(f"Path '{rel_path}' escapes the repository root.")
        return candidate

    # ---------- exploration tools ----------

    def list_directory(self, path: str = ".", max_depth: int = 3) -> str:
        """Recursively list files/folders under `path`, skipping noisy dirs."""
        root = self._resolve(path)
        lines = []

        def walk(dir_path: Path, depth: int):
            if depth > max_depth:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
            except NotADirectoryError:
                return
            for entry in entries:
                if entry.name in IGNORE_DIRS:
                    continue
                rel = entry.relative_to(self.repo_root)
                indent = "  " * depth
                if entry.is_dir():
                    lines.append(f"{indent}{rel}/")
                    walk(entry, depth + 1)
                else:
                    lines.append(f"{indent}{rel}")

        if root.is_file():
            return str(root.relative_to(self.repo_root))
        walk(root, 0)
        return "\n".join(lines) if lines else "(empty directory)"

    def read_file(self, path: str) -> str:
        """Return file content with 1-indexed line numbers (like `cat -n`)."""
        target = self._resolve(path)
        if not target.exists():
            return f"ERROR: file not found: {path}"
        if not target.is_file():
            return f"ERROR: not a file: {path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{i+1:>5}\t{line}" for i, line in enumerate(text.splitlines()))
        return numbered or "(empty file)"

    def search_files(self, pattern: str, glob: str = "**/*") -> str:
        """Grep-like recursive text search across the repo (skips ignored dirs)."""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"

        hits = []
        for file_path in self.repo_root.glob(glob):
            if not file_path.is_file():
                continue
            if any(part in IGNORE_DIRS for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = file_path.relative_to(self.repo_root)
                    hits.append(f"{rel}:{i}: {line.strip()}")
            if len(hits) > 300:
                break
        if not hits:
            return "No matches found."
        return "\n".join(hits[:300])

    # ---------- modification tools ----------

    def write_file(self, path: str, content: str) -> str:
        """Create a new file or fully overwrite an existing one."""
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        """Replace exactly one occurrence of old_str with new_str in a file."""
        target = self._resolve(path)
        if not target.exists():
            return f"ERROR: file not found: {path}"
        text = target.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            return f"ERROR: old_str not found in {path}. Nothing changed."
        if count > 1:
            return f"ERROR: old_str matches {count} locations in {path}; " \
                   f"make old_str more specific (include more surrounding context)."
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return f"OK: applied edit to {path}"

    # ---------- read-only git tool ----------

    def git(self, subcommand: str, args: str = "") -> str:
        """Run a whitelisted, read-only git subcommand inside the repo."""
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            return f"ERROR: git subcommand '{subcommand}' is not allowed. " \
                   f"Allowed: {sorted(ALLOWED_GIT_SUBCOMMANDS)}"
        cmd = ["git", subcommand] + (args.split() if args else [])
        result = subprocess.run(
            cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=15
        )
        return (result.stdout or result.stderr or "(no output)")[:8000]


# ---------- Mistral tool schema (OpenAI-style function-calling definitions) ----------
# Mistral's chat.complete(tools=...) expects: {"type": "function", "function": {name, description, parameters}}
# (same shape as OpenAI function calling - Anthropic's {name, description, input_schema} shape doesn't apply here).

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders under a path in the repo, recursively (skips node_modules/.git). Use '.' for repo root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, default '.'"},
                    "max_depth": {"type": "integer", "description": "Max recursion depth, default 3"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file (with line numbers) at a path relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Regex search across repo files (like grep -r). Use to find where a symbol/route/model is defined or used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern"},
                    "glob": {"type": "string", "description": "Glob to restrict search, e.g. '**/*.js'. Default '**/*'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or completely overwrite an existing file, with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Surgically replace one exact snippet (old_str) with new_str inside an existing file. old_str must match exactly once - include enough surrounding context to make it unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Run a read-only git command inside the repo, e.g. subcommand='diff' to review your own changes so far.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subcommand": {"type": "string", "description": "one of: status, diff, log, show, branch"},
                    "args": {"type": "string", "description": "extra args, e.g. '--stat'"},
                },
                "required": ["subcommand"],
            },
        },
    },
]
