"""Run the built-in search and URL tools in isolated ADK invocations."""

from contextlib import aclosing
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from google.adk.agents import Agent, RunConfig
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools import google_search, url_context
from google.genai import errors, types

from .config import generation_config
from .prompts import research_prompt

URL_RETRIEVAL = "public_url_retrieval"
SUCCESS = types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS


class UrlReadingGemini(Gemini):
    """Preserve retrieval status that ADK drops when converting Gemini responses."""

    async def generate_content_async(self, llm_request, stream=False):
        # URL reading uses a dedicated, non-streaming Runner with one native tool.
        raw = await self.api_client.aio.models.generate_content(
            model=self.model, contents=llm_request.contents, config=llm_request.config
        )
        response = LlmResponse.create(raw)
        if raw.candidates and (metadata := raw.candidates[0].url_context_metadata):
            response.custom_metadata = {
                URL_RETRIEVAL: metadata.model_dump(mode="json", exclude_none=True)
            }
        yield response


def public_url(url: str) -> str | None:
    """Accept a public HTTP(S) URL for Gemini's remote URL Context tool."""
    url = url.strip()
    if "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        try:
            address = ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".localhost", ".local", ".internal")):
                return None
        else:
            if not address.is_global or address.is_multicast:
                return None
    except ValueError:
        return None
    return parsed._replace(path=parsed.path or "/", fragment="").geturl()


def source_agents(model, url_model, thinking_level):
    """Keep each native tool on its own root agent for ADK compatibility."""
    return (
        Agent(
            name="public_search",
            model=model,
            instruction=research_prompt("search"),
            generate_content_config=generation_config(thinking_level),
            tools=[google_search],
        ),
        Agent(
            name="public_url_reader",
            model=url_model,
            instruction=research_prompt("url_reader"),
            generate_content_config=generation_config(thinking_level),
            tools=[url_context],
        ),
    )


def event_text(event) -> str:
    """Extract visible text without exposing model reasoning parts."""
    parts = event.content.parts if event.content else []
    return "".join(part.text for part in parts or [] if part.text and not part.thought)


async def source_response(agent: Agent, message: str):
    """Return a single source response without adding it to the case conversation."""
    runner = InMemoryRunner(agent=agent, app_name=agent.name)
    try:
        await runner.session_service.create_session(
            app_name=agent.name, user_id="local", session_id="source"
        )
        events = runner.run_async(
            user_id="local",
            session_id="source",
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            run_config=RunConfig(max_llm_calls=1),
        )
        async with aclosing(events):
            async for event in events:
                if event.author == agent.name and event.is_final_response():
                    return event
    finally:
        await runner.close()
    return None


def source_error(error: Exception) -> str:
    """Report transport or API failure without exposing request details."""
    if isinstance(error, errors.APIError):
        return f"Gemini API request failed (HTTP {error.code})."
    if isinstance(error, httpx.TimeoutException):
        return "Gemini API request timed out."
    return "Could not reach the Gemini API."
