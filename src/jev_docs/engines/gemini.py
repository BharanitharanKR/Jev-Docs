"""Google Gemini decision engine using official Gemini REST API with structured JSON output."""

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


class GeminiEngine:
    name = "gemini"

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        *,
        api_key: str | None = None,
        timeout: float = 60,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ProviderError("Set GEMINI_API_KEY to use the Gemini engine.")
        # Normalize model name
        if "/" in model:
            model = model.split("/")[-1]
        self.model = model
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

    async def _generate(self, prompt: str, schema_instruction: str, task: str) -> tuple[dict, list[RequestRecord]]:
        started = time.perf_counter()
        full_content = f"{prompt}\n\n{schema_instruction}\nRespond ONLY in valid JSON format."
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [
                {
                    "parts": [{"text": full_content}]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0,
            },
        }

        try:
            res = await self.client.post(
                url,
                headers={"Content-Type": "application/json"},
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
                    f"Gemini API request failed ({code}): {error_msg}", requests=[record]
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
            raise ProviderError(f"Gemini connection error: {exc}", requests=[record]) from None

        try:
            candidate = res_data["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            usage = res_data.get("usageMetadata", {})
        except (KeyError, IndexError) as exc:
            record = RequestRecord(
                provider=self.name,
                model=self.model,
                task=task,
                status="invalid",
                elapsed_ms=elapsed_ms,
            )
            raise ProviderError(f"Gemini returned unexpected structure: {exc}", requests=[record]) from None

        record = RequestRecord(
            provider=self.name,
            model=self.model,
            task=task,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            cost_usd=0.0,
            cost_status="reported",
        )

        try:
            cleaned = text.strip()
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
                f"Gemini returned invalid JSON: {text[:200]}", requests=[record]
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
            'Output JSON schema:\n{"category": "<category_id>"}'
        )
        data, records = await self._generate(prompt, schema_instruction, "classify")
        category = data.get("category")
        if category not in rules.criteria:
            raise ProviderError(f"Gemini returned unknown category '{category}'.", requests=records)
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
            "Output JSON schema:\n"
            '{"segments": [{"category": "<category_id>", "start": 1, "end": 2}, {"category": "<category_id>", "start": 3, "end": 3}, ...]}'
        )
        data, records = await self._generate(prompt, schema_instruction, "split")
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
                f"Gemini returned invalid segment coverage: {exc}", requests=records
            ) from None
        return decisions, records
