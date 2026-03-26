"""
GitHub Copilot CLI backend.

Uses `gh copilot explain` to send a plain-text prompt and capture the response.
The CLI is interactive by default, so we craft a self-contained question and
parse the plain-text output.
"""
import json
import re
import subprocess
import textwrap

from src.llm.base import BackendFactory, LLMBackend


@BackendFactory.register("copilot")
class CopilotBackend(LLMBackend):
    """Calls `gh copilot explain` to get an LLM response."""

    # Timeout (seconds) for a single gh copilot call.
    TIMEOUT = 60

    def ask(self, prompt: str) -> str:
        """
        Send `prompt` to the Copilot CLI and return the plain-text reply.

        gh copilot explain reads from stdin, so we pipe the prompt in.
        We strip any ANSI escape codes from the output.
        """
        try:
            result = subprocess.run(
                ["gh", "copilot", "explain", prompt],
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT,
            )
            raw = result.stdout + result.stderr
            return self._strip_ansi(raw).strip()
        except subprocess.TimeoutExpired:
            return ""
        except FileNotFoundError:
            raise RuntimeError(
                "gh CLI not found. Install it from https://cli.github.com/ "
                "and authenticate with `gh auth login`."
            )

    @staticmethod
    def _strip_ansi(text: str) -> str:
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)
