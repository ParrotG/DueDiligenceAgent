# Due Diligence Agent

A Google ADK assistant for preliminary corporate due diligence using public sources. It can identify a company, propose investigation topics, research approved topics in parallel, review material claims, and draft or revise findings in the conversation.

## Setup

Install [uv](https://docs.astral.sh/uv/) and sync the Python 3.11 environment.

```bash
uv sync --locked
cp -n .env.local.example .env.local
```

Set `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the project root's `.env.local`. The default model is `gemini-4-neon` with medium thinking. Edit [config.toml](config.toml) to change the model, thinking level, or research budgets, or pass `--model` for a one-off model override.

## Run an investigation

```bash
uv run --locked python -m due_diligence.cli --trace
```

Use `/reset` to start a new case and `/quit` to exit. The CLI retains recent conversation history and any pending plan in the current process. With `--trace`, it prints the workflow stages and tool calls.

Try these requests in the same session:

```text
Propose two narrow research topics about IANA example domains using only https://www.iana.org/help/example-domains. Wait for my approval.
Approve the plan and execute both topics.
Review whether the IANA page supports treating example.com as available for registration.
Turn the latest findings into a short draft.
```

Use `--prompt` to run a single request. In interactive use, the CLI presents a temporary local link for Google Search results. Use `--preview-search` to open this preview in single-request or piped mode. For document reading, provide a direct public URL. If URL Context reports only the redirected address, retry with that address.

## Development

```bash
uv run --locked python -m unittest discover -s tests -v
```

The tests use Google ADK's runtime and graph with scripted model responses, so they run without a Gemini key or network access. The graph is defined in [workflow.py](due_diligence/workflow.py), and agent instructions live in [due_diligence/prompts](due_diligence/prompts). See the [architecture guide in Chinese](docs/architecture.zh-CN.md) for data flow, tool behavior, and extension points.
