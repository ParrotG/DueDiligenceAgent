# Due Diligence Agent

A Google ADK assistant that researches companies from public sources and produces a cited draft for review. It moves through a Research Brief, a Research Planner, parallel Research Workers, and a Research Evaluator. Each worker checks its own evidence and follows up on useful gaps before handing off a subquestion report.

## Setup

Install [uv](https://docs.astral.sh/uv/) and sync the Python 3.11 environment.

```bash
uv sync --locked
cp -n .env.local.example .env.local
```

Set `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the project root's `.env.local`. The default model is `gemini-4-neon` with medium thinking. Edit [config.toml](config.toml) to change research budgets or pass `--model` to override the model for one run.

## Run an investigation

```bash
uv run --locked python -m due_diligence.cli --trace
```

Describe the subject, the decision the research should inform, and any scope or source restrictions. The assistant asks a focused question when the brief identifies material ambiguity. It retains the brief, plan, subquestion reports, and draft for the life of the current case.

```text
Research whether IANA's example.com domain is available for registration. Use the official page at https://www.iana.org/help/example-domains.
/brief
/revise Put the answer in the first sentence and keep the citation.
/approve
```

Use `/brief` to view the recorded brief, `/restart [scope]` to return to the brief, `/reset` to clear the case, or `/quit` to exit. You can also give draft feedback in plain language. Use `--prompt` for a single request.

The stages can use Google Search for discovery, URL Context for interpretation, and HTTPX to fetch public HTML, text, JSON, XML, and text-bearing PDF originals. Grounded Google Search responses appear in a temporary local preview. Use `--preview-search` to keep it available in batch mode. The native ADK SkillToolset loads unmodified upstream [research skills](skills/UPSTREAM.md) and their bundled reference files on demand.

## Development

```bash
uv run --locked python -m unittest discover -s tests -v
```

Tests run without live API calls. The [Chinese architecture guide](docs/architecture.zh-CN.md) describes the graph, research state, source tools, and skill loading.
