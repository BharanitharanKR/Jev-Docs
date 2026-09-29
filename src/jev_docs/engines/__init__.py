"""Hosted text-only decision engines."""


def make_engine(name: str = "jev", model: str | None = None):
    if name == "jev":
        from .jev import JevEngine

        return JevEngine(model=model or "jev-1.13.0")
    if name == "openrouter":
        from .openrouter import OpenRouterEngine

        return OpenRouterEngine(model=model or "liquid/lfm-2.5-2.6b:free")
    if name == "openai":
        from .openai import OpenAIEngine

        return OpenAIEngine(model=model or "gpt-5.6-luna")
    from ..errors import JevDocsError

    raise JevDocsError("Engine must be 'jev', 'openrouter', or 'openai'.")
