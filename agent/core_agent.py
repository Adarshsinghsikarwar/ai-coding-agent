"""
core_agent.py
-------------
A minimal, dependency-light agent loop:
  1. Send messages + tool schemas to Claude.
  2. If Claude requests tool calls, execute them locally against RepoTools
     and feed the results back.
  3. Repeat until Claude responds with plain text (no more tool calls) or
     a max-turn safety limit is hit.

This is intentionally framework-free (no LangChain/AutoGPT) so the control
flow is fully visible and auditable - useful both for correctness and for
explaining the design in an interview.
"""

import os
from anthropic import Anthropic
from .tools import RepoTools, TOOL_SCHEMAS

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-5-20250929")
MAX_TURNS = 25  # safety valve per phase, to avoid infinite tool-call loops


class Agent:
    def __init__(self, repo_root: str, verbose: bool = True):
        self.client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.tools = RepoTools(repo_root)
        self.verbose = verbose

    def _log(self, *args):
        if self.verbose:
            print(*args)

    def _dispatch_tool(self, name: str, tool_input: dict) -> str:
        """Route a tool_use block to the matching RepoTools method."""
        method = getattr(self.tools, name, None)
        if method is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return str(method(**tool_input))
        except Exception as e:
            return f"ERROR calling {name}: {e}"

    def run_phase(self, system_prompt: str, user_message: str, allow_tools: bool = True) -> str:
        """
        Run one phase to completion (a bounded tool-use conversation) and
        return Claude's final plain-text answer for that phase.
        """
        messages = [{"role": "user", "content": user_message}]
        tools = TOOL_SCHEMAS if allow_tools else []

        for turn in range(MAX_TURNS):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                # Claude produced a final text answer for this phase.
                final_text = "".join(b.text for b in response.content if b.type == "text")
                return final_text

            # Log + execute each requested tool call, append assistant turn + results.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in tool_uses:
                self._log(f"  [tool] {block.name}({block.input})")
                result_text = self._dispatch_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text[:6000],  # keep context lean
                })
            messages.append({"role": "user", "content": tool_results})

        return "ERROR: hit MAX_TURNS without a final answer for this phase."
