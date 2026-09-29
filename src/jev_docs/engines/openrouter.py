"""OpenRouter decision engine using chat completions with structured JSON output."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx2

from ..errors import ContextLimitError, DocumentError, ProviderError
from ..schemas import PageDecision, ParsedDocument, RequestRecord, RuleSet
from .base import (
    BOUNDARY_POLICY,
    UNTRUSTED,
    CategoryDecision,
    classification_instructions,
    page_state,
)


class OpenRouterEngine:
    name = "openrouter"

    def __init__(
        self,
        model: str = "liquid/lfm-2.5-2.6b:free",
        *,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 60,
    ):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ProviderError("Set OPENROUTER_API_KEY to use the OpenRouter engine.")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = httpx2.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def checked_payload(document: ParsedDocument, rules: RuleSet) -> dict:
        payload = {"document": page_state(document.pages), "categories": rules.criteria}
        if len(json.dumps(payload).encode()) > 500_000:
            raise ContextLimitError(
                "The supported request-size limit was exceeded; no content was truncated."
            )
        return payload

    async def _chat(self, prompt: str, schema_instruction: str, task: str) -> tuple[dict, list[RequestRecord]]:
        started = time.perf_counter()
        full_content = f"{prompt}\n\n{schema_instruction}\nRespond ONLY with valid JSON."
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/BharanitharanKR/Jev-Docs",
            "X-Title": "DocJev",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise document classification and segmentation engine. Always reply strictly in valid JSON.",
                },
                {"role": "user", "content": full_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

        try:
            res = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if res.status_code != 200:
                code = str(res.status_code)
                error_msg = res.text[:200]
                record = RequestRecord(
                    provider=self.name,
                    model=self.model,
                    task=task,
                    status="error",
                    elapsed_ms=elapsed_ms,
                    error_code=code,
                )
                raise ProviderError(
                    f"OpenRouter request failed ({code}): {error_msg}", requests=[record]
                )
            res_data = res.json()
        except ProviderError:
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            record = RequestRecord(
                provider=self.name,
                model=self.model,
                task=task,
                status="error",
                elapsed_ms=elapsed_ms,
                error_code=type(exc).__name__,
            )
            raise ProviderError(f"OpenRouter connection error: {exc}", requests=[record]) from None

        choice = res_data["choices"][0]
        content = choice["message"]["content"]
        usage = res_data.get("usage", {})
        record = RequestRecord(
            provider=self.name,
            model=res_data.get("model", self.model),
            task=task,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=0.0 if ":free" in self.model else None,
            cost_status="reported" if ":free" in self.model else "unknown",
        )

        try:
            # Strip markdown json block formatting if present
            cleaned = content.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())
        except (ValueError, TypeError):
            record.status = "invalid"
            raise ProviderError(
                f"OpenRouter returned invalid JSON: {content[:200]}", requests=[record]
            ) from None

        return data, [record]

    async def classify(self, document: ParsedDocument, rules: RuleSet) -> tuple[CategoryDecision, list[RequestRecord]]:
        instructions = classification_instructions(rules)
        categories_desc = "\n".join(f"- {c}: {desc}" for c, desc in rules.criteria.items())
        pages_text = "\n".join(
            f"--- PAGE {p.number} ---\n{p.text}" for p in document.pages
        )
        prompt = (
            f"{instructions}\n\n"
            f"Defined Categories:\n{categories_desc}\n\n"
            f"Document Content ({document.page_count} pages):\n{pages_text}"
        )
        schema_instruction = (
            f"Select the single best category from: {list(rules.criteria.keys())}.\n"
            'Output schema:\n{"category": "<category_id>"}'
        )
        data, records = await self._chat(prompt, schema_instruction, "classify")
        category = data.get("category")
        if category not in rules.criteria:
            raise ProviderError(f"OpenRouter returned unknown category '{category}'.", requests=records)
        return CategoryDecision(category), records

    async def split(
        self, document: ParsedDocument, rules: RuleSet, *, boundary_threshold: float = 0.5
    ) -> tuple[list[PageDecision], list[RequestRecord]]:
        del boundary_threshold
        instruction = (
            UNTRUSTED + BOUNDARY_POLICY + "Split the full packet into ordered contiguous segments. "
            "Use inclusive 1-based start/end pages. Cover every page exactly once (pages 1 to "
            f"{document.page_count}) with no overlaps or gaps. "
            "Separate two adjacent source documents even if they share a category. "
            + rules.instructions
            + " "
            + rules.splitting_instructions
        )
        categories_desc = "\n".join(f"- {c}: {desc}" for c, desc in rules.criteria.items())
        pages_summary = "\n".join(
            f"--- PAGE {p.number} ---\n{p.text[:800]}" for p in document.pages
        )
        prompt = (
            f"{instruction}\n\n"
            f"Defined Categories:\n{categories_desc}\n\n"
            f"Pages in Packet (Total: {document.page_count}):\n{pages_summary}"
        )
        schema_instruction = (
            "Output schema:\n"
            '{"segments": [{"category": "<category_id>", "start": 1, "end": 2}, {"category": "<category_id>", "start": 3, "end": 3}, ...]}'
        )
        data, records = await self._chat(prompt, schema_instruction, "split")
        decisions: list[PageDecision] = []
        try:
            for segment in data.get("segments", []):
                start, end = segment["start"], segment["end"]
                cat = segment["category"]
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 1
                    or end < start
                    or end > document.page_count
                    or cat not in rules.criteria
                ):
                    raise ValueError(f"Invalid segment: {segment}")
                decisions.extend(
                    PageDecision(page=p, category=cat, starts_document=(p == start))
                    for p in range(start, end + 1)
                )
            if [d.page for d in decisions] != list(range(1, document.page_count + 1)):
                raise ValueError("Missing or overlapping pages in split output")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"OpenRouter returned invalid segment coverage: {exc}", requests=records
            ) from None
        return decisions, records
