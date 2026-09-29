"""Hosted text-only decision engines."""


def make_engine(name: str = "jev", model: str | None = None):
    if name in ("jev", "openrouter"):
        from .jev import JevEngine

        default_model = "typesafe/jev-1.13" if name == "openrouter" else "jev-1.13.0"
        return JevEngine(model=model or default_model)
    if name == "openai":
        from .openai import OpenAIEngine

        return OpenAIEngine(model=model or "gpt-5.6-luna")
    from ..errors import JevDocsError

    raise JevDocsError("Engine must be 'jev', 'openrouter', or 'openai'.")
