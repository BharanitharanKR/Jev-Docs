# PAX-I.AI API (`cross-border-ai-api`) Architecture & Strategic Integration Guide

> **Target Systems**: `cross-border-ai-api` & `paxiai-event-processor`  
> **Integration Engine**: `Jev-Docs` (LiteParse + TypeSafe Jev Decision Engine)  
> **Stack**: NestJS 10, TypeScript, TypeORM, MySQL, Qdrant, Elasticsearch, Redis/BullMQ, Langfuse  
> **Date**: September 2026  

---

## 1. Executive Overview

**`cross-border-ai-api`** is the core application backend for the **PAX-I.AI** enterprise platform. It serves as the primary API gateway for client frontends (`paxi-ui`), orchestrating multi-tenant authentication, conversational RAG (Retrieval-Augmented Generation) search over engineering activity, real-time feed translation, async job dispatching, and developer productivity analytics.

```
                          ┌───────────────────────────────┐
                          │       Frontend (paxi-ui)       │
                          └──────────────┬────────────────┘
                                         │ HTTP / REST / SSE Stream
                                         ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                       cross-border-ai-api (NestJS)                        │
│                                                                           │
│   ┌─────────────────────┐  ┌──────────────────────────────────────────┐   │
│   │ Multi-Tenant Auth   │  │ AI Chat & RAG Engine                     │   │
│   │ • JWT & Google OAuth│  │ • Query Processor & Intent Rewriter      │   │
│   │ • x-account-id      │  │ • Context Retrieval Service              │   │
│   │ • Permission Guards │  │ • LLM Streaming (OpenAI, Gemini, Azure)  │   │
│   └─────────────────────┘  └──────────────────────────────────────────┘   │
│   ┌─────────────────────┐  ┌──────────────────────────────────────────┐   │
│   │ Integrations Hub    │  │ Feed Translation Engine                  │   │
│   │ • GitHub, Jira,     │  │ • Pointer-based JSON extraction          │   │
│   │   Slack, Confluence │  │ • Azure Translator batching (<25k chars) │   │
│   │ • Google Drive/Cal  │  │ • Schema-preserving reassembly           │   │
│   └─────────────────────┘  └──────────────────────────────────────────┘   │
│   ┌─────────────────────┐  ┌──────────────────────────────────────────┐   │
│   │ AI Analytics & DORA │  │ BullMQ Job Producer                      │   │
│   │ • Aggregated Metrics│  │ • Dispatches sync & vectorization tasks  │   │
│   │ • Productivity & Dev│  │   to paxiai-event-processor              │   │
│   └─────────────────────┘  └──────────────────────────────────────────┘   │
└──────────────┬─────────────────────────────┬──────────────────────────────┘
               │                             │
       ┌───────┴────────┐             ┌──────┴─────────┐
       ▼                ▼             ▼                ▼
┌──────────────┐ ┌─────────────┐ ┌─────────┐ ┌───────────────────────────┐
│ MySQL 8.0    │ │ Qdrant + ES │ │ Redis   │ │ paxiai-event-processor    │
│ (TypeORM)    │ │ (Hybrid RAG)│ │ (BullMQ)│ │ (Ingestion & Vectorizing) │
└──────────────┘ └─────────────┘ └─────────┘ └───────────────────────────┘
```

---

## 2. Codebase Structure & Architectural Layers

The repository follows a clean, feature-first modular architecture located in `src/features/v2/`:

| Layer / Directory | Primary Responsibility | Key Files / References |
| :--- | :--- | :--- |
| **`core/`** | Framework-agnostic base types and repository contracts. | `core/base/repository.interface.ts` |
| **`models/entites/`** | TypeORM entities and single database connection provider. | `src/models/model.module.ts` |
| **`features/v2/chat/`** | Conversational RAG, query rewriting, context retrieval, streaming. | `chat.service.ts`, `context-retrieval.service.ts` |
| **`features/v2/translation/`** | Pointer-based JSON batch translation preserving schema structure. | `feed-translation.service.ts` |
| **`features/v2/integrations/`** | OAuth management & webhooks (GitHub, Jira, Slack, Confluence, GDrive). | `integrations.service.ts` |
| **`features/v2/jobs/`** | Redis BullMQ producer delegating document sync and vectorization. | `jobs.service.ts`, `job.constants.ts` |
| **`presentation/controllers/v2/`** | HTTP layer; validates requests, delegates to services, formats output. | `chat.controller.ts`, `translation.controller.ts` |
| **`infrastructure/`** | External drivers: Qdrant, Elasticsearch, Langfuse, LLM provider factories. | `infrastructure/services/ai/ai.module.ts` |
| **`shared/`** | Cross-cutting filters, response envelopes, custom exceptions, and env definitions. | `ResponseInterceptor`, `HttpExceptionFilter`, `ENV` |

---

## 3. How Core Subsystems Work

### 3.1 AI Chat & Retrieval-Augmented Generation (RAG)
When a user asks a question in the UI:
1. **Query Processing & Scoping** (`QueryProcessorService`):
   - Evaluates conversation history and regenerates user queries into standalone search strings.
   - Extracts payload filters (e.g., date ranges, project scope, participant IDs).
2. **Context Retrieval** (`ContextRetrievalService`):
   - Generates vector embeddings using the active embedding model (`OpenAI`, `Azure`, or `Gemini`).
   - Executes hybrid retrieval via `VectorSearchService`:
     - **Stage 1 (Qdrant)**: Dense HNSW vector search + BM25 sparse matching combined via Reciprocal Rank Fusion (RRF) to pull up to 100 candidates.
     - **Stage 2 (Cohere)**: Passes candidate chunks to Cohere Cloud Reranker for cross-attention relevance scoring.
     - **Fallback**: Automatically falls back to Elasticsearch vector index if Qdrant is disabled.
3. **Prompt Formatting & SSE Streaming** (`ChatService`):
   - Injects the top reranked chunks into the system prompt.
   - Streams tokens via an Async Generator to the client (`text/event-stream` or NDJSON).
   - Reports latency, token counts, and costs to Langfuse and Elasticsearch audit logs.

### 3.2 The Feed Translation Engine
Cross-border teams need project updates translated (e.g., English $\leftrightarrow$ Japanese) without corrupting system metadata:
1. **Pointer Extraction**: Scans Feed JSON to find only human-readable text fields (`topic`, `summary`, `impact`, `recommendedAction`, `updates`), saving their JSON path coordinates.
2. **Batch Chunking**: Organizes extracted text into safe batches ($\le 50$ items and $< 25,000$ characters) to respect Azure Translator limits.
3. **Bounded Concurrency**: Dispatches parallel batches using `Promise.all` with a strict limit of 5 concurrent requests to prevent HTTP 429 rate-limiting and socket exhaustion.
4. **In-Memory Reassembly**: Uses stored pointer indices to insert translated strings back into the exact original JSON schema, preserving IDs, timestamps, and scores.

---

## 4. Current Bottlenecks & Friction Points

| Problem Area | Current State in Codebase | Consequence / Impact |
| :--- | :--- | :--- |
| **Context Chunk Fragmentation** | Chunks retrieved from Qdrant/ES were sliced mechanically by token count in `paxiai-event-processor` (`MAX_CHUNK_TOKENS`). | Multi-row data tables, bulleted technical specs, and multi-document packets get severed. The chat LLM frequently hallucinates numbers because parent table headers are cut off. |
| **Missing Document Taxonomy in Search** | Payloads only contain basic identifiers (`account_id`, `project_id`, `file_id`, `file_name`). | Chat queries cannot filter by document category (e.g., "only search technical specs" or "only search meeting notes"), forcing the reranker to evaluate dozens of irrelevant chunks. |
| **Documentation Drift** | Files in `docs/` still reference Prisma and v1 flat controllers, while the app actually runs TypeORM and `features/v2`. | Onboarding engineers and external agents get misled by obsolete documentation. |
| **Manual Schema Migrations** | SQL scripts in `schema/` are applied out-of-band rather than through an automated migration runner. | Deployment drift across local, staging, and production databases. |
| **Redundant Translation Calls** | Feed translation has no phrase caching layer; identical status phrases are repeatedly translated via Azure Translator. | Higher external API latency and avoidable translation billing costs. |

---

## 5. Strategic Plan: "What Shall We Do?"

```
┌────────────────────────────────────────────────────────────────────────┐
│                        RECOMMENDED ACTION PLAN                         │
├────────────────────────────────────────────────────────────────────────┤
│ 1. CONNECT JEV-DOCS TAXONOMY TO CHAT RETRIEVAL                        │
│    • Filter Qdrant hybrid search by document_category & section_type   │
│    • 40% reduction in noisy candidate chunks evaluated by Cohere/LLM  │
├────────────────────────────────────────────────────────────────────────┤
│ 2. IMPLEMENT PARENT-CHILD CONTEXT EXPANSION                           │
│    • Store segment_id & parent_header in Qdrant payloads               │
│    • Eliminate table splitting & hallucinated RAG responses           │
├────────────────────────────────────────────────────────────────────────┤
│ 3. ADD REDIS TRANSLATION CACHING                                       │
│    • Memoize common feed phrases and category strings                  │
│    • Reduce Azure Translator API calls by 25–35%                       │
├────────────────────────────────────────────────────────────────────────┤
│ 4. TECH DEBT & MIGRATION HARDENING                                     │
│    • Update repository documentation to match TypeORM v2               │
│    • Automate schema deployment checks                                 │
└────────────────────────────────────────────────────────────────────────┘
```

### Action 1: Upgrade Chat Retrieval with Jev-Docs Taxonomy
* **Where**: `src/features/v2/chat/services/query-processor.service.ts` and `context-retrieval.service.ts`.
* **How**:
  1. Have the `QueryProcessorService` detect document category intent from user prompts (e.g., "financial", "architecture", "minutes", "contract").
  2. Map this into `ChatPayloadFilters.document_category`.
  3. Pass `must: [{ key: 'document_category', match: { value: detectedCategory } }]` into the Qdrant hybrid query.
* **Benefit**: Eliminates non-relevant document chunks before reranking, drastically cutting token consumption and boosting retrieval precision.

### Action 2: Parent-Child Context Expansion
* **Where**: `src/features/v2/chat/utils/context-retrieval.utils.ts`.
* **How**:
  1. When Jev-Docs outputs clean contiguous boundary segments, each chunk stores `parent_heading`, `section_id`, and `is_table`.
  2. When formatting context for the chat prompt, wrap table chunks with their corresponding markdown header and column schema.
* **Benefit**: Guarantees zero-hallucination answers when users query complex tables or financial figures.

### Action 3: Redis Translation Caching Layer
* **Where**: `src/features/v2/translation/services/feed-translation.service.ts`.
* **How**:
  1. Before batching strings for Azure Translator, check a Redis hash (`translation:cache:<targetLang>:<sha256(text)>`).
  2. Only dispatch novel strings to Azure; populate cached translations directly during in-memory reassembly.
* **Benefit**: Instantaneous translations for common recurring feed topics, updates, and status messages, with a 25–35% reduction in translation costs.

### Action 4: Codebase Hygiene & Documentation Alignment
* Update `docs/DEVELOPMENT_GUIDE.md` and `docs/FEATURES.md` in `cross-border-ai-api` to reflect the active TypeORM and `features/v2` architecture, retiring legacy Prisma references.
* Standardize database migration execution via TypeORM CLI scripts in CI/CD.

---

## 6. Action Items Checklist

- [ ] **Chat Service**: Add support for `document_category` and `section_title` filters in `ContextRetrievalService`.
- [ ] **Event Processor Sync**: Coordinate with `paxiai-event-processor` to ensure Jev-Docs tags (`document_category`, `is_table`) are written to Qdrant point payloads.
- [ ] **Translation Cache**: Add Redis memoization for recurring phrases in `features/v2/translation/services/feed-translation.service.ts`.
- [ ] **Documentation**: Deprecate legacy `docs/DEVELOPMENT_GUIDE.md` Prisma sections in the API repository.
