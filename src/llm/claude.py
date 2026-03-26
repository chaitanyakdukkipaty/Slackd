"""
Claude CLI backend stub.

To activate:
  1. Install the Anthropic CLI: `pip install anthropic` or follow
     https://docs.anthropic.com/claude/docs/quickstart
  2. Set ANTHROPIC_API_KEY in your environment.
  3. Change `llm.backend` in config.yaml to `claude`.
  4. Replace the NotImplementedError body below with the actual call, e.g.:

     import anthropic
     client = anthropic.Anthropic()
     message = client.messages.create(
         model="claude-opus-4-5",
         max_tokens=1024,
         messages=[{"role": "user", "content": prompt}]
     )
     return message.content[0].text
"""
from src.llm.base import BackendFactory, LLMBackend


@BackendFactory.register("claude")
class ClaudeBackend(LLMBackend):
    def ask(self, prompt: str) -> str:
        raise NotImplementedError(
            "Claude backend is not yet implemented. "
            "See the module docstring for instructions."
        )
