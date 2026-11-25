"""Run a multi-turn investigation and collect its user-facing results."""

import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx
from google.adk.agents import RunConfig
from google.adk.models.google_llm import Gemini
from google.adk.runners import InMemoryRunner
from google.genai import errors, types

from .config import load_settings, make_client
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

from .workflow import Investigation, make_workflow


@dataclass
class SearchResult:
    """Carry a grounded response to the current user for immediate display."""

    text: str
    metadata: types.GroundingMetadata


@dataclass
class Turn:
    """Return the coordinator's answer and diagnostics for one request."""

    answer: str = ""
    stages: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    searches: list[SearchResult] = field(default_factory=list)
    source_calls: int = 0


class ResearchAssistant:
    """Maintain one case conversation and run its ADK team."""

    def __init__(self, model=None, *, url_model=None, max_source_calls=None, settings=None):
        self.settings = settings or load_settings()
        self._current = ContextVar[tuple[Turn, Investigation]]("investigation")
        self._lock = asyncio.Lock()
        self._history: list[dict[str, str]] = []
        self._pending_plan = None
        self._max_source_calls = (
            self.settings.workflow.max_source_calls
            if max_source_calls is None else max_source_calls
        )
        self._client = None
        model = model or self.settings.gemini.model
        if isinstance(model, str):
            self._client = make_client(self.settings)
            if url_model is None:
                url_model = UrlReadingGemini(model=model, client=self._client)
            model = Gemini(model=model, client=self._client)
        self._search_agent, self._url_agent = source_agents(
            model, url_model or model, self.settings.gemini.thinking_level
        )
        self.agent = make_workflow(
            model, self.search_public_web, self.read_public_url,
            self._current.get, self.settings, self._max_source_calls,
        )
        self._runner = InMemoryRunner(agent=self.agent, app_name=self.agent.name)

    async def ask(self, message: str) -> Turn:
        """Run one graph invocation using prior turns and any plan awaiting approval."""
        message = message.strip()
        if not message:
            raise ValueError("Enter a research request.")
        async with self._lock:
            turn = Turn()
            work = Investigation(message, list(self._history), self._pending_plan)
            token = self._current.set((turn, work))
            service = self._runner.session_service
            session = None
            try:
                session = await service.create_session(app_name=self.agent.name, user_id="local")
                events = self._runner.run_async(
                    user_id=session.user_id,
                    session_id=session.id,
                    new_message=types.Content(
                        role="user", parts=[types.Part(text=message)]
                    ),
                    run_config=RunConfig(max_llm_calls=self.settings.workflow.max_model_calls),
                )
                async with aclosing(events):
                    async for event in events:
                        turn.calls.extend(
                            call.name for call in event.get_function_calls() if call.name
                        )
            finally:
                self._current.reset(token)
                if session is not None:
                    await service.delete_session(
                        app_name=self.agent.name, user_id=session.user_id, session_id=session.id
                    )
            self._pending_plan = work.pending_plan
            if turn.answer:
                self._history.append({
                    "user": message,
                    "assistant": turn.answer[:self.settings.workflow.history_answer_chars],
                })
                self._history = self._history[-self.settings.workflow.history_turns:]
        return turn

    async def close(self) -> None:
        """Release the ADK runner and the explicitly configured Gemini client."""
        try:
            await self._runner.close()
        finally:
            if self._client is not None:
                await self._client.aio.aclose()
                self._client.close()

    def _reserve_source_call(self, name: str) -> bool:
        turn = self._current.get()[0]
        if turn.source_calls >= self._max_source_calls:
            return False
        turn.source_calls += 1
        turn.calls.append(name)
        return True

    async def search_public_web(self, query: str) -> dict:
        """Search public sources for the current user using a focused, public query."""
        if not query.strip() or len(query) > self.settings.sources.max_query_chars:
            return {"status": "invalid_query"}
        if not self._reserve_source_call("google_search"):
            return {"status": "source_call_limit"}
        try:
            event = await source_response(self._search_agent, query.strip())
        except (errors.APIError, httpx.RequestError) as error:
            return {"status": "error", "message": source_error(error)}
        if not event or not event.grounding_metadata or not event_text(event).strip():
            return {"status": "no_grounded_response"}

        self._current.get()[0].searches.append(
            SearchResult(text=event_text(event), metadata=event.grounding_metadata)
        )
        # Search output stays outside the coordinator and specialist session history.
        return {"status": "grounded_response_available_for_user"}

    async def read_public_url(self, url: str, question: str) -> dict:
        """Read an independently identified public URL and answer a focused question."""
        checked_url = public_url(url) if len(url) <= self.settings.sources.max_url_chars else None
        if checked_url is None:
            return {"status": "invalid_public_url"}
        if not question.strip() or len(question) > self.settings.sources.max_question_chars:
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
