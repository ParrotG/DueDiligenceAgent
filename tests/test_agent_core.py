"""Exercise ADK graph routing, research iterations, and native tool configuration."""

from collections import defaultdict, deque
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from google.adk import Workflow
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from due_diligence.runtime import ResearchAssistant
from due_diligence.sources import SUCCESS, URL_RETRIEVAL, UrlReadingGemini, public_url
from due_diligence.workflow import Gaps, Plan

URL = "https://example.com/filings/annual-report.pdf"
LEAD = "A search-only lead that must stay out of the case context."


def message(text, **metadata):
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]), **metadata
    )


def decision(action, objective="Review Example Holdings.", **fields):
    return message(Plan(action=action, objective=objective, **fields).model_dump_json())


def gaps(*questions):
    return message(Gaps(follow_up=list(questions)).model_dump_json())


def call(name, **args):
    part = types.Part.from_function_call(name=name, args=args)
    part.function_call.id = f"test-{name}"
    return LlmResponse(content=types.Content(role="model", parts=[part]))


def read_call(url=URL):
    return call("read_public_url", url=url, question="Who controls the company?")


def retrieval(url=URL, status=SUCCESS.value):
    return {URL_RETRIEVAL: {"url_metadata": [
        {"retrieved_url": url, "url_retrieval_status": status}
    ]}}


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
                "Classify the current user request": "planner",
                "Confirm the legal name": "entity",
                "Summarize the scoped entity": "overview",
                "Research one assigned question": "topic",
                "Try to find independently sourced evidence": "review",
                "Decide whether the reviewer": "gaps",
                "Respond in English": "compose",
            }
            role = next(role for marker, role in roles.items() if marker in instruction)
        self._requests[role].append(llm_request.model_copy(update={
            "contents": list(llm_request.contents)
        }))
        response = self._scripts[role].popleft()
        response.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=5, total_token_count=15
        )
        yield response


class CoreTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = ScriptedGemini(model="gemini-test")
        self.assistant = ResearchAssistant(self.model)
        self.addAsyncCleanup(self.assistant.close)

    async def test_can_start_with_review_then_switch_to_overview(self):
        self.assertIsInstance(self.assistant.agent, Workflow)
        self.model.add("planner", decision("review"), decision("overview"))
        self.model.add("review", message("No source establishes voting control."))
        self.model.add("gaps", gaps())
        self.model.add("overview", message("Preliminary overview."))
        self.model.add("compose", message("The control claim is unresolved."), message("Overview."))

        first = await self.assistant.ask("Review the control claim before profiling.")
        second = await self.assistant.ask("Switch to a preliminary overview.")
        self.assertEqual(first.stages, ["plan", "review", "assess", "compose"])
        self.assertEqual(second.stages, ["plan", "overview", "compose"])
        self.assertIn("unresolved", first.answer)
        self.assertIn("before profiling", str(self.model._requests["planner"][1].contents))

    async def test_plan_waits_and_approval_runs_the_topics_that_were_shown(self):
        self.model.add(
            "planner",
            decision("investigate", topics=["Voting control", "Regulatory disclosures"], needs_approval=True),
            decision("investigate", topics=["Unapproved extra work"], uses_pending_plan=True),
        )
        self.model.add("topic", message("First finding."), message("Second finding."))
        self.model.add("compose", message("The two approved topics were investigated."))

        proposed = await self.assistant.ask("Suggest topics to investigate.")
        self.assertEqual(proposed.stages, ["plan", "propose"])
        self.assertIn("Regulatory disclosures", proposed.answer)
        self.assertFalse(self.model._requests["topic"])
        approved = await self.assistant.ask("Go ahead with the plan.")
        self.assertEqual(approved.stages, ["plan", "investigate", "compose"])
        self.assertIn("Regulatory disclosures", str(self.model._requests["planner"][1].contents))
        requests = str([request.contents for request in self.model._requests["topic"]])
        self.assertIn("Voting control", requests)
        self.assertIn("Regulatory disclosures", requests)
        self.assertNotIn("Unapproved extra work", requests)

    async def test_review_rechecks_new_questions_until_the_follow_up_limit(self):
        self.model.add("planner", decision("investigate", topics=["Control"], review_findings=True))
        self.model.add(
            "topic", message("Control at filing date."), message("Later filing changed control."),
            message("Registry confirms the later filing."),
        )
        self.model.add(
            "review", message("A later filing may contradict the claim."),
            message("Check the registry for independent confirmation."),
            message("Revise the claim."),
        )
        self.model.add(
            "gaps", gaps("control", "Check the later filing", "Check the later filing"),
            gaps("Check the registry"),
        )
        self.model.add("compose", message("The later filing supersedes the initial finding."))

        result = await self.assistant.ask("Investigate the control claim.")
        self.assertEqual(result.stages, [
            "plan", "investigate", "review", "assess", "investigate", "review",
            "assess", "investigate", "review", "compose",
        ])
        self.assertEqual(len(self.model._requests["topic"]), 3)
        inputs = str(self.model._requests["compose"][0].contents)
        self.assertIn("Later filing changed control", inputs)
        self.assertIn("Registry confirms the later filing", inputs)

    async def test_topic_uses_url_context_and_returns_findings_to_synthesis(self):
        self.model.add("planner", decision("investigate", topics=[f"Read {URL}"]))
        self.model.add("topic", read_call(), message("50% of the voting rights."))
        self.model.add("url", message("50% of the voting rights.", custom_metadata=retrieval()))
        self.model.add("compose", message("The filing discloses voting control."))

        result = await self.assistant.ask("Investigate the ownership filing.")
        self.assertEqual(result.stages, ["plan", "investigate", "compose"])
        self.assertCountEqual(result.calls, ["read_public_url", "url_context"])
        self.assertIn("50% of the voting rights.", str(self.model._requests["topic"][1].contents))
        self.assertIn("50% of the voting rights.", str(self.model._requests["compose"][0].contents))

    async def test_search_is_native_and_does_not_enter_case_context(self):
        self.model.add("planner", decision("investigate"), decision("answer"))
        self.model.add("topic", call("search_public_web", query="Example Holdings official filings"),
                       message("A discovery result is available."))
        self.model.add("compose", message("A discovery result is available."),
                       message("Please provide the original filing."))
        self.model.add("search", message(
            LEAD, grounding_metadata=types.GroundingMetadata(
                search_entry_point=types.SearchEntryPoint(rendered_content="<div>Search</div>")
            )
        ))

        result = await self.assistant.ask("Find public leads.")
        await self.assistant.ask("What do we still need?")
        self.assertCountEqual(result.calls, ["search_public_web", "google_search"])
        self.assertEqual(result.searches[0].text, LEAD)
        for role in ("planner", "topic", "compose"):
            for request in self.model._requests[role]:
                self.assertNotIn(LEAD, str(request.contents))

    async def test_url_response_requires_success_metadata_for_requested_url(self):
        for metadata in (
            None,
            retrieval(status="URL_RETRIEVAL_STATUS_PAYWALL"),
            retrieval(url="https://other.example.org/"),
        ):
            with self.subTest(metadata=metadata):
                self.model.add("planner", decision("investigate"))
                self.model.add("topic", read_call(), message("The source is unavailable."))
                self.model.add("url", message("Unverified synthetic content.", custom_metadata=metadata))
                self.model.add("compose", message("Unable to verify."))
                await self.assistant.ask("Read the ownership source.")
                response = str(self.model._requests["topic"][-1].contents)
                self.assertIn("unverified", response)
                self.assertNotIn("Unverified synthetic content.", response)

    async def test_invalid_url_does_not_consume_source_budget(self):
        self.model.add("planner", decision("investigate"))
        self.model.add("topic", read_call("http://127.0.0.1/a"), message("Choose a public URL."))
        self.model.add("compose", message("Choose a public URL."))
        result = await self.assistant.ask("Read this location.")
        self.assertEqual(result.calls, ["read_public_url"])
        self.assertFalse(self.model._requests["url"])
        self.assertEqual(result.source_calls, 0)

    async def test_parallel_topics_share_source_budget(self):
        limited = ResearchAssistant(self.model, max_source_calls=1)
        self.addAsyncCleanup(limited.close)
        self.model.add("planner", decision("investigate", topics=["First", "Second"]))
        self.model.add("topic", read_call(), read_call("https://example.org/filing"),
                       message("First source checked."), message("Second source checked."))
        self.model.add("url", message("The provider did not verify this source."))
        self.model.add("compose", message("The lookup budget has been reached."))

        result = await limited.ask("Inspect two filings.")
        self.assertEqual(result.source_calls, 1)
        self.assertCountEqual(result.calls, ["url_context", "read_public_url", "read_public_url"])
        self.assertTrue(any("source_call_limit" in str(request.contents)
                            for request in self.model._requests["topic"]))

    async def test_clarification_and_revision_skip_research(self):
        self.model.add(
            "planner",
            decision("clarify", message="Which jurisdiction?"),
            decision("revise", objective="Shorten the executive summary."),
        )
        self.model.add("compose", message("Shorter executive summary."))
        first = await self.assistant.ask("Identify Example Holdings.")
        second = await self.assistant.ask("Shorten my draft's executive summary without more research.")
        self.assertEqual(first.stages, ["plan", "clarify"])
        self.assertEqual(second.stages, ["plan", "compose"])
        self.assertEqual(first.answer, "Which jurisdiction?")
        self.assertEqual(second.calls, [])

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
        self.assertEqual(results[0].custom_metadata, retrieval())
        self.assertIsNotNone(generate.await_args.kwargs["config"].tools[0].url_context)


class UrlTests(TestCase):
    def test_public_urls_reject_credentials_private_hosts_and_non_http_schemes(self):
        for url in (
            "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://[::1]/",
            "http://10.0.0.1/", "https://example.com.internal/",
            "https://user:password@example.com/", "https://example.com:8080/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(public_url(url))
        self.assertEqual(public_url(f"{URL}#page=4"), URL)
        self.assertEqual(public_url("https://example.com"), "https://example.com/")
