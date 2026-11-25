"""Run a multi-turn investigation and collect its user-facing results."""

import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx
from google.adk.agents import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import errors, types

from .agents import make_coordinator
from .sources import (
    SUCCESS,
    URL_RETRIEVAL,
    UrlReadingGemini,
    event_text,
    public_url,
    source_agents,
    source_error,
    source_response,
)

DEFAULT_MODEL = "gemini-3.1-flash-lite"


@dataclass
class SearchResult:
    """Carry a grounded response to the current user for immediate display."""

    text: str
    metadata: types.GroundingMetadata


@dataclass
class Turn:
    """Return the coordinator's answer and diagnostics for one request."""

    answer: str = ""
    calls: list[str] = field(default_factory=list)
    searches: list[SearchResult] = field(default_factory=list)
    source_calls: int = 0


class ResearchAssistant:
    """Maintain one case conversation and run its ADK team."""

    def __init__(self, model=DEFAULT_MODEL, *, url_model=None, max_source_calls=8):
        self._turn = ContextVar[Turn]("research_turn")
        self._lock = asyncio.Lock()
        self._max_source_calls = max_source_calls
        if url_model is None:
            url_model = UrlReadingGemini(model=model) if isinstance(model, str) else model
        self._search_agent, self._url_agent = source_agents(model, url_model)
        self.agent = make_coordinator(model, self.search_public_web, self.read_public_url)
        self._runner = InMemoryRunner(
            agent=self.agent, app_name=self.agent.name
        )

    async def ask(self, message: str) -> Turn:
        """Continue the case, returning visible output and tool names for this turn."""
        message = message.strip()
        if not message:
            raise ValueError("Enter a research request.")
        async with self._lock:
            turn = Turn()
            token = self._turn.set(turn)
            try:
                session_key = {
                    "app_name": self.agent.name, "user_id": "local", "session_id": "case"
                }
                service = self._runner.session_service
                if await service.get_session(**session_key) is None:
                    await service.create_session(**session_key)
                events = self._runner.run_async(
                    user_id="local",
                    session_id="case",
                    new_message=types.Content(
                        role="user", parts=[types.Part(text=message)]
                    ),
                    run_config=RunConfig(max_llm_calls=16),
                )
                async with aclosing(events):
                    async for event in events:
                        turn.calls.extend(
                            call.name for call in event.get_function_calls() if call.name
                        )
                        if event.author == self.agent.name and event.is_final_response():
                            turn.answer = event_text(event) or turn.answer
            finally:
                self._turn.reset(token)
        return turn

    async def close(self) -> None:
        """Release the ADK runner when the investigation ends."""
        await self._runner.close()

    def _reserve_source_call(self, name: str) -> bool:
        turn = self._turn.get()
        if turn.source_calls >= self._max_source_calls:
            return False
        turn.source_calls += 1
        turn.calls.append(name)
        return True

    async def search_public_web(self, query: str) -> dict:
        """Search public sources for the current user using a focused, public query."""
        if not query.strip() or len(query) > 1200:
            return {"status": "invalid_query"}
        if not self._reserve_source_call("google_search"):
            return {"status": "source_call_limit"}
        try:
            event = await source_response(self._search_agent, query.strip())
        except (errors.APIError, httpx.RequestError) as error:
            return {"status": "error", "message": source_error(error)}
        if not event or not event.grounding_metadata or not event_text(event).strip():
            return {"status": "no_grounded_response"}

        self._turn.get().searches.append(
            SearchResult(text=event_text(event), metadata=event.grounding_metadata)
        )
        # Search output stays outside the coordinator and specialist session history.
        return {"status": "grounded_response_available_for_user"}

    async def read_public_url(self, url: str, question: str) -> dict:
        """Read an independently identified public URL and answer a focused question."""
        checked_url = public_url(url)
        if checked_url is None:
            return {"status": "invalid_public_url"}
        if not question.strip() or len(question) > 2000:
            return {"status": "invalid_question"}
        if not self._reserve_source_call("url_context"):
            return {"status": "source_call_limit"}

        try:
            event = await source_response(
                self._url_agent, f"Public URL: {checked_url}\nQuestion: {question.strip()}"
            )
        except (errors.APIError, httpx.RequestError) as error:
            return {"status": "error", "url": checked_url, "message": source_error(error)}

        metadata = (event.custom_metadata or {}).get(URL_RETRIEVAL, {}) if event else {}
        statuses = {
            public_url(item["retrieved_url"]): item.get("url_retrieval_status")
            for item in metadata.get("url_metadata", [])
            if item.get("retrieved_url")
        }
        status = statuses.get(checked_url)
        if status != SUCCESS:
            return {"status": "unverified", "url": checked_url, "retrieval_status": status}
        findings = event_text(event).strip()
        if not findings:
            return {"status": "no_findings", "url": checked_url}
        return {"status": "retrieved", "url": checked_url, "findings": findings}
