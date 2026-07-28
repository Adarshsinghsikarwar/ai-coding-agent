"""
core_agent.py
-------------
A minimal, dependency-light agent loop, now powered by Mistral AI:
  1. Send messages + tool schemas to Mistral's chat.complete().
  2. If Mistral requests tool calls, execute them locally against RepoTools
     and feed the results back as role="tool" messages.
  3. Repeat until Mistral responds with plain text (no more tool calls) or
     a max-turn safety limit is hit.

This is intentionally framework-free (no LangChain/AutoGPT) so the control
flow is fully visible and auditable - useful both for correctness and for
explaining the design in an interview.
"""

import os
import json
import time

try:
    # Most published versions of the SDK expose this at the top level.
    from mistralai import Mistral
except ImportError:
    # Some builds (e.g. mistralai==2.7.2) nest it under mistralai.client instead.
    from mistralai.client import Mistral

try:
    from mistralai.models import SDKError
except ImportError:
    from mistralai.client.errors.sdkerror import SDKError

from .tools import RepoTools, TOOL_SCHEMAS

MODEL = os.environ.get("AGENT_MODEL", "mistral-large-latest")
MAX_TURNS = 25  # safety valve per phase, to avoid infinite tool-call loops
MAX_RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BASE_DELAY_SECONDS = 5  # doubles each retry: 5s, 10s, 20s, 40s...


class Agent:
    def __init__(self, repo_root: str, verbose: bool = True):
        self.client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
        self.tools = RepoTools(repo_root)
        self.verbose = verbose

    def _log(self, *args):
        if self.verbose:
            print(*args)

    def _dispatch_tool(self, name: str, tool_input: dict) -> str:
        """Route a tool call to the matching RepoTools method."""
        method = getattr(self.tools, name, None)
        if method is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return str(method(**tool_input))
        except Exception as e:
            return f"ERROR calling {name}: {e}"

    def _complete_with_retry(self, messages, tools):
        """
        Call chat.complete(), automatically waiting and retrying if Mistral
        returns a 429 (rate limit) - common on free/trial API keys, which
        allow far fewer requests per minute than a paid plan.
        """
        delay = RATE_LIMIT_BASE_DELAY_SECONDS
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return self.client.chat.complete(
                    model=MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto" if tools else "none",
                )
            except SDKError as e:
                is_rate_limited = "429" in str(e) or "rate_limited" in str(e).lower()
                if not is_rate_limited or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                self._log(f"  [rate limit] hit 429, waiting {delay}s before retry "
                          f"{attempt + 1}/{MAX_RATE_LIMIT_RETRIES}...")
                time.sleep(delay)
                delay *= 2  # exponential backoff

    def run_phase(self, system_prompt: str, user_message: str, allow_tools: bool = True,
                  max_turns: int = None) -> str:
        """
        Run one phase to completion (a bounded tool-use conversation) and
        return Mistral's final plain-text answer for that phase.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        tools = TOOL_SCHEMAS if allow_tools else None
        turns_allowed = max_turns or MAX_TURNS
        tool_call_history = []  # for debugging if we hit the turn limit

        for turn in range(turns_allowed):
            response = self._complete_with_retry(messages, tools)
            choice = response.choices[0].message
            tool_calls = choice.tool_calls or []

            if not tool_calls:
                # Mistral produced a final text answer for this phase.
                return choice.content or ""

            # Log + execute each requested tool call, append assistant turn + results.
            messages.append({
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": choice.tool_calls,
            })

            for call in tool_calls:
                name = call.function.name
                raw_args = call.function.arguments
                # Mistral's SDK returns arguments as either a dict or a JSON string
                # depending on version - handle both defensively.
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw_args or {}

                self._log(f"  [tool] {name}({args})")
                result_text = self._dispatch_tool(name, args)
                tool_call_history.append(f"{name}({args}) -> {result_text[:200]}")
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": result_text[:6000],  # keep context lean
                    "tool_call_id": call.id,
                })

        # Hit the turn limit without a final answer - surface *what actually
        # happened* instead of a bare error, so it's diagnosable from the
        # artifact alone (did it loop re-reading files? keep failing an edit?).
        recent = "\n".join(tool_call_history[-10:])
        return (
            f"ERROR: hit MAX_TURNS ({turns_allowed}) without a final answer for this phase.\n\n"
            f"Last {min(10, len(tool_call_history))} tool calls before the limit:\n{recent}\n\n"
            f"Likely cause: the model kept calling tools (e.g. re-reading files, or a failing "
            f"edit_file it kept retrying) instead of finishing. Try increasing max_turns for this "
            f"phase, or check above whether an edit_file call is repeatedly failing."
        )
