# Customer Support Email Router

Learning prototype: reads customer emails, works out intent, automates safe cases, drafts
risky ones and routes the rest to humans. **LLMs interpret, code decides, humans own risk.**

Requirements: [`CLAUDE.md`](CLAUDE.md) · Decisions: [`docs/decisions.md`](docs/decisions.md)

## Setup

```bash
uv sync                      # Python 3.13, installs deps into .venv
cp .env.example .env         # fill in keys only for hosted providers you use
```

## Configuration

All tunables live in `config/` (YAML). Layers, later wins:

1. `config/base.yaml` and the files it `includes`
2. `config/local.yaml` (optional, gitignored; see `local.yaml.example`)
3. files listed in `ROUTER_CONFIG_OVERLAYS` (`:`-separated)
4. `--config <file>` options, in order

```bash
uv run router config validate                           # fails with every problem found
uv run router config validate --require-secrets         # also require API key env vars
uv run router config show -c config/eval/anthropic.yaml # effective config, secrets removed
```

Safety invariants (billing/payment/legal/abuse never AUTO; legal/abuse never drafted) are
enforced in code; a config that tries to break them will not load.

### Switching LLM provider

Each LLM step (`understand`, `summarize`, `compose`, `judge`) names a provider and model in
`config/llm.yaml`. Default: Qwen 3.8 27B on LM Studio (`http://localhost:1234/v1`).
To switch, override `llm.steps.<step>.provider/model` in `config/local.yaml` or an overlay;
see `config/eval/anthropic.yaml`. No code changes.

## Tests

```bash
uv run pytest
```

_Sections for mocks, processing the inbox, review CLI and evals are added as milestones land._
