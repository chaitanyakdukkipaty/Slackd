"""
OpenAI CLI backend stub.

To activate:
  1. Install the OpenAI CLI: `pip install openai`
  2. Set OPENAI_API_KEY in your environment.
  3. Change `llm.backend` in config.yaml to `openai`.
  4. Replace the NotImplementedError body below with the actual call, e.g.:

     from openai import OpenAI
     client = OpenAI()
     response = client.chat.completions.create(
         model="gpt-4o",
         messages=[{"role": "user", "content": prompt}]
     )
     return response.choices[0].message.content
"""
from src.llm.base import BackendFactory, LLMBackend


@BackendFactory.register("openai")
class OpenAIBackend(LLMBackend):
    def ask(self, prompt: str) -> str:
        raise NotImplementedError(
            "OpenAI backend is not yet implemented. "
            "See the module docstring for instructions."
        )
