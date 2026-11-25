"""Read application defaults and load Gemini credentials from the local file."""

import tomllib
from pathlib import Path

from dotenv import dotenv_values
from google import genai
from google.genai import types
from pydantic import BaseModel, NonNegativeInt, PositiveInt

ROOT = Path(__file__).resolve().parents[1]


class GeminiConfig(BaseModel):
    model: str
    timeout_seconds: PositiveInt
    thinking_level: types.ThinkingLevel | None = None


class WorkflowConfig(BaseModel):
    max_model_calls: PositiveInt
    max_source_calls: PositiveInt
    evaluator_source_reserve: NonNegativeInt
    max_topics: PositiveInt
    max_follow_up_rounds: NonNegativeInt
    history_turns: PositiveInt
    history_answer_chars: PositiveInt


class SourcesConfig(BaseModel):
    max_query_chars: PositiveInt
    max_question_chars: PositiveInt
    max_url_chars: PositiveInt
    preview_wait_seconds: PositiveInt
    fetch_timeout_seconds: PositiveInt
    fetch_max_bytes: PositiveInt
    fetch_max_chars: PositiveInt
    fetch_max_redirects: NonNegativeInt


class Settings(BaseModel):
    gemini: GeminiConfig
    workflow: WorkflowConfig
    sources: SourcesConfig


def load_settings(path: Path = ROOT / "config.toml") -> Settings:
    """Load validated defaults without accessing credentials."""
    with path.open("rb") as file:
        return Settings.model_validate(tomllib.load(file))


def make_client(settings: Settings, env_file: Path = ROOT / ".env.local") -> genai.Client:
    """Use credentials from the local file without exporting them to the process."""
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
    key = (values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise ValueError("Set GEMINI_API_KEY or GOOGLE_API_KEY in .env.local.")
    return genai.Client(
        api_key=key,
        vertexai=False,
        http_options=types.HttpOptions(timeout=settings.gemini.timeout_seconds * 1000),
    )


def generation_config(thinking_level: types.ThinkingLevel | None) -> types.GenerateContentConfig:
    """Build the per-agent generation settings without enabling thought output."""
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level)
        if thinking_level else None,
    )
