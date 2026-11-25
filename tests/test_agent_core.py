"""Exercise real ADK routing with deterministic model responses and native tools."""

from collections import defaultdict, deque
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from due_diligence.runtime import ResearchAssistant
from due_diligence.sources import SUCCESS, URL_RETRIEVAL, UrlReadingGemini, public_url

URL = "https://example.com/filings/annual-report.pdf"
LEAD = "A search-only lead that must stay out of the case context."


def message(text, **metadata):
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]), **metadata
    )


def call(name, **args):
    part = types.Part.from_function_call(name=name, args=args)
    part.function_call.id = f"test-{name}"
    return LlmResponse(content=types.Content(role="model", parts=[part]))


def read_call(url=URL):
    return call("read_public_url", url=url, question="Who controls the company?")


class ScriptedGemini(BaseLlm):
    """Supply deterministic responses after ADK has constructed each model request."""

    _scripts = PrivateAttr(default_factory=lambda: defaultdict(deque))
    _requests = PrivateAttr(default_factory=lambda: defaultdict(list))

    def add(self, role, *responses):
        self._scripts[role].extend(responses)

    async def generate_content_async(self, llm_request, stream=False):
        instruction = llm_request.config.system_instruction
        tools = llm_request.config.tools or []
        if any(tool.google_search is not None for tool in tools):
            role = "search"
        elif any(tool.url_context is not None for tool in tools):
            role = "url"
        else:
            roles = {
                "Confirm the legal name": "entity",
                "Research the assigned question": "topic",
                "Try to find evidence": "review",
                "You coordinate": "coordinator",
            }
            role = next(role for marker, role in roles.items() if marker in instruction)
        self._requests[role].append(llm_request.model_copy(update={"contents": list(llm_request.contents)}))
        yield self._scripts[role].popleft()


class CoreTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = ScriptedGemini(model="gemini-test")
        self.assistant = ResearchAssistant(self.model)
        self.addAsyncCleanup(self.assistant.close)

    async def test_user_can_start_with_review_and_change_scope_next_turn(self):
        self.model.add(
            "coordinator",
            call("claim_review", request="Challenge the control claim first."),
            message("The control claim is unresolved."),
            call("topic_research", request="Research the selected governance topic."),
            message("Here is the requested governance finding."),
        )
        self.model.add("review", message("No source establishes voting control."))
        self.model.add("topic", message("Governance finding."))

        first = await self.assistant.ask("Review the control claim before profiling.")
        second = await self.assistant.ask("Switch to the governance topic only.")
        self.assertEqual(first.calls, ["claim_review"])
        self.assertEqual(second.calls, ["topic_research"])
        self.assertIn("unresolved", first.answer)
        self.assertIn("before profiling", str(self.model._requests["coordinator"][2].contents))

    async def test_topic_can_use_url_context_and_return_findings_to_coordinator(self):
        self.model.add(
            "coordinator",
            call("topic_research", request=f"Read the filing at {URL}."),
            message("The filing discloses voting control."),
        )
        self.model.add("topic", read_call(), message("50% of the voting rights."))
        self.model.add("url", message(
            "50% of the voting rights.",
            custom_metadata={URL_RETRIEVAL: {"url_metadata": [{
                "retrieved_url": URL, "url_retrieval_status": SUCCESS.value,
            }]}},
        ))

        result = await self.assistant.ask("Investigate the ownership filing.")
        self.assertEqual(result.calls, ["topic_research", "url_context"])
        topic_inputs = self.model._requests["topic"][1].contents
        self.assertIn("50% of the voting rights.", str(topic_inputs))
        self.assertIn("voting control", result.answer)

    async def test_search_is_native_and_search_output_stays_outside_case_context(self):
        self.model.add(
            "coordinator",
            call("search_public_web", query="Example Holdings official filings"),
            message("A discovery result is available."),
            message("Please provide the original filing."),
        )
        self.model.add(
            "search",
            message(
                LEAD,
                grounding_metadata=types.GroundingMetadata(
                    search_entry_point=types.SearchEntryPoint(rendered_content="<div>Search</div>")
                ),
            ),
        )
        result = await self.assistant.ask("Find public leads.")
        await self.assistant.ask("What do we still need?")
        self.assertEqual(result.calls, ["search_public_web", "google_search"])
        self.assertEqual(result.searches[0].text, LEAD)
        for request in self.model._requests["coordinator"]:
            self.assertNotIn(LEAD, str(request.contents))

    async def test_url_response_requires_retrieval_metadata_for_requested_url(self):
        for metadata, expected in (
            (None, "unverified"),
            ({URL_RETRIEVAL: {"url_metadata": [{
                "retrieved_url": URL, "url_retrieval_status": "URL_RETRIEVAL_STATUS_PAYWALL",
            }]}}, "unverified"),
            ({URL_RETRIEVAL: {"url_metadata": [{
                "retrieved_url": "https://other.example.org/", "url_retrieval_status": SUCCESS.value,
            }]}}, "unverified"),
        ):
            with self.subTest(metadata=metadata):
                self.model.add("coordinator", read_call(), message("The source is unavailable."))
                self.model.add("url", message("Unverified synthetic content.", custom_metadata=metadata))
                await self.assistant.ask("Read the ownership source.")
                response = self.model._requests["coordinator"][-1].contents
                self.assertIn(expected, str(response))
                self.assertNotIn("Unverified synthetic content.", str(response))

    async def test_url_rejection_does_not_call_model_or_consume_source_budget(self):
        self.model.add("coordinator", read_call("http://127.0.0.1/a"), message("Choose a public URL."))
        result = await self.assistant.ask("Read this location.")
        self.assertEqual(result.calls, ["read_public_url"])
        self.assertFalse(self.model._requests["url"])
        self.assertEqual(result.source_calls, 0)

    async def test_specific_entity_request_can_use_entity_agent_directly(self):
        self.model.add(
            "coordinator",
            call("entity_research", request="Disambiguate Example Holdings."),
            message("Which jurisdiction did you mean?"),
        )
        self.model.add("entity", message("Two entities share that name."))
        result = await self.assistant.ask("Identify Example Holdings.")
        self.assertEqual(result.calls, ["entity_research"])
        self.assertIn("jurisdiction", result.answer)

    async def test_source_budget_is_shared_by_specialist_and_coordinator(self):
        limited = ResearchAssistant(self.model, max_source_calls=1)
        self.addAsyncCleanup(limited.close)
        self.model.add(
            "coordinator",
            call("topic_research", request=f"Read the filing at {URL}."),
            read_call("https://example.org/other-filing"),
            message("One source was assessed and the lookup budget has been reached."),
        )
        self.model.add("topic", read_call(), message("Filing unavailable."))
        self.model.add("url", message("The provider did not verify this source."))

        result = await limited.ask("Inspect the two filings.")
        self.assertEqual(result.calls, ["topic_research", "url_context", "read_public_url"])
        self.assertEqual(result.source_calls, 1)
        self.assertIn("source_call_limit", str(self.model._requests["coordinator"][-1].contents))

    async def test_native_url_model_preserves_provider_metadata(self):
        raw = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="Ownership filing.")]),
            url_context_metadata=types.UrlContextMetadata(url_metadata=[types.UrlMetadata(
                retrieved_url=URL, url_retrieval_status=SUCCESS,
            )]),
        )])
        generate = AsyncMock(return_value=raw)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with patch.object(UrlReadingGemini, "api_client", client):
            reader = UrlReadingGemini(model="gemini-test")
            request = LlmRequest(model="gemini-test", config=types.GenerateContentConfig(
                tools=[types.Tool(url_context=types.UrlContext())]
            ))
            results = [result async for result in reader.generate_content_async(request)]
        metadata = results[0].custom_metadata[URL_RETRIEVAL]
        self.assertEqual(metadata["url_metadata"][0]["url_retrieval_status"], SUCCESS.value)
        self.assertIsNotNone(generate.await_args.kwargs["config"].tools[0].url_context)


class UrlTests(TestCase):
    def test_public_urls_reject_credentials_private_hosts_and_non_http_schemes(self):
        for url in (
            "file:///etc/passwd",
            "http://localhost/",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "https://example.com.internal/",
            "https://user:password@example.com/",
            "https://example.com:8080/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(public_url(url))
        self.assertEqual(public_url(f"{URL}#page=4"), URL)
        self.assertEqual(public_url("https://example.com"), "https://example.com/")
