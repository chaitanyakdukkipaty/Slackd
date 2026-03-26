"""
GitHub Copilot CLI backend.

Uses `gh copilot explain -- -p <prompt>` for non-interactive scripting.
The `--` separator prevents gh from interpreting Copilot flags, and `-p`
passes the prompt without opening an interactive session.
"""
import re
import subprocess

from src.llm.base import BackendFactory, LLMBackend

# Footer lines appended by the Copilot CLI after the response content.
_FOOTER_PATTERNS = re.compile(
    r"^(Total usage est|API time spent|Total session time|Total code changes|"
    r"Breakdown by AI model|claude-|gpt-|o[0-9]-)",
    re.IGNORECASE,
)


@BackendFactory.register("copilot")
class CopilotBackend(LLMBackend):
    """Calls `gh copilot explain -- -p <prompt>` for non-interactive LLM responses."""

    TIMEOUT = 90

    def ask(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                ["gh", "copilot", "explain", "--", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT,
            )
            raw = result.stdout + result.stderr
            return self._clean(raw)
        except subprocess.TimeoutExpired:
            return ""
        except FileNotFoundError:
            raise RuntimeError(
                "gh CLI not found. Install it from https://cli.github.com/ "
                "and authenticate with `gh auth login`."
            )

    @staticmethod
    def _clean(text: str) -> str:
        """Strip ANSI codes and the usage-stats footer appended by the CLI."""
        # Remove ANSI escape sequences.
        ansi = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        text = ansi.sub("", text)

        # Drop trailing footer lines (stats block after a blank line).
        lines = text.splitlines()
        clean: list[str] = []
        for line in lines:
            if _FOOTER_PATTERNS.match(line.strip()):
                break
            clean.append(line)

        return "\n".join(clean).strip()
