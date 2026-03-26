"""
Abstract base class for LLM backends.
"""
from abc import ABC, abstractmethod


class LLMBackend(ABC):
    """All LLM backends must implement ask()."""

    @abstractmethod
    def ask(self, prompt: str) -> str:
        """Send a plain-text prompt and return the response as a string."""
        ...


class BackendFactory:
    """Returns the correct LLMBackend instance based on a name string."""

    _registry: dict[str, type] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a backend class under a given name."""
        def decorator(backend_cls: type):
            cls._registry[name] = backend_cls
            return backend_cls
        return decorator

    @classmethod
    def get(cls, name: str) -> "LLMBackend":
        # Lazy import so stubs don't fail if optional deps are missing.
        from src.llm import copilot, claude, openai_cli  # noqa: F401
        if name not in cls._registry:
            raise ValueError(
                f"Unknown LLM backend '{name}'. "
                f"Available: {list(cls._registry.keys())}"
            )
        return cls._registry[name]()
