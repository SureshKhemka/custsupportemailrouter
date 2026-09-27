# Decisions log (DE-4)

| ID | Date | Decision | Why / notes |
|---|---|---|---|
| D-000 | 2026-09-27 | Stack: Python 3.13 + uv, Pydantic v2, YAML layered config, FastAPI mocks (one process per service), httpx, SQLite, in-house LLM client (`openai_compat` + `anthropic` adapters) with record/replay, Typer + Rich CLI, pytest, in-house eval runner. Plain-Python pipeline, no agent framework. | Approved proposal for DE-1. |
| D-001 | 2026-09-27 | **English only** for now. `app.supported_languages: [en]`. Other languages (including Hindi and Hinglish) are routed to a human as unsupported (FR-5). Language support stays configurable for later. | User decision. The dataset still contains non-English emails (DS-2); their expected handling is "route: unsupported language". |
| D-002 | 2026-09-27 | For `return_request`, the router only **creates the return authorisation** (AUTO if eligible). It never issues refunds for returns: the e-commerce platform refunds after pickup. The `refund_issuance` action (DRAFT, optional AUTO threshold) applies only to damaged / wrong / never-arrived remedies. Return replies must not promise a refund date or claim a refund was issued. | User decision. |
| D-003 | 2026-09-27 | Default model for all LLM steps: **Qwen 3.8 27B via LM Studio** (OpenAI-compatible API). The judge is configured separately (EV-8) and can point at a different model through an overlay. | User decision. LM Studio model id `qwen/qwen3.8-27b`; it is a config value (`llm.steps.*.model`). |
| D-004 | 2026-09-27 | Email files are **JSON**, one file per email; format documented in `docs/email-format.md` (intake milestone). | User decision. |
| D-005 | 2026-09-27 | Git remote: `https://github.com/SureshKhemka/custsupportemailrouter`. | User decision. |
| D-006 | 2026-09-27 | Default operating mode is **`shadow`** (process fully, send nothing). Eval and demo overlays switch to `live` (writing to the mock outbox). | Safest default; "humans own risk". |
| D-007 | 2026-09-27 | Beyond FR-19: `CLOSE` (no reply) is allowed only for `spam_or_auto`; legal/abuse must be exactly `ROUTE_NO_DRAFT`; the routing matrix must cover exactly the taxonomy intents. Enforced in code. | Prevents silently dropping a real customer email via config. |
| D-008 | 2026-09-27 | Config files may not contain secret-looking values (e.g. `sk-...`); `api_key_env` must be an environment-variable **name**. Validation fails otherwise. | CF-3 defence against pasting a key into YAML. |
| D-009 | 2026-09-27 | Repeat-contact escalation defaults to the **2nd contact** on the same issue within 14 days. | FR-16 "repeat contact"; configurable. |
