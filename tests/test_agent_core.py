"""Exercise the ADK research graph with scripted models and real tool dispatch."""

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
from due_diligence.workflow import Assessment, Brief, Feedback, ResearchPlan, Task

URL = "https://example.com/filings/annual-report.pdf"
LEAD = "A search-only lead that must stay out of case context."
CONTROL = Task(title="Control", question="Who controls the company?", rationale="Material ownership.")
LEGAL = Task(title="Disputes", question="What material disputes were disclosed?", rationale="Legal risks.")


def message(text, **metadata):
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]), **metadata
    )


def structured(result):
    return message(result.model_dump_json())


def brief(**fields):
    return structured(Brief(
        subject="Example Holdings", purpose="Screen ownership risk.", overview="A listed company.",
        identifiers=["12345"], aliases=["Example Group"], brands=["Example Retail"],
        ownership_and_control=["Voting rights require verification."], **fields,
    ))


def plan(*tasks):
    return structured(ResearchPlan(objective="Assess material risks.", tasks=list(tasks)))


def assessment(decision="sufficient", report="Checked the original.", next_step=""):
    return structured(Assessment(
        decision=decision, assessment="Source quality reviewed.", report=report, next_step=next_step,
    ))


def call(tool_name, **args):
    part = types.Part.from_function_call(name=tool_name, args=args)
    part.function_call.id = f"test-{tool_name}"
    return LlmResponse(content=types.Content(role="model", parts=[part]))


def read_call(url=URL):
    return call("read_public_url", url=url, question=CONTROL.question)


def retrieval(url=URL, status=SUCCESS.value):
    return {URL_RETRIEVAL: {"url_metadata": [
        {"retrieved_url": url, "url_retrieval_status": status}
    ]}}


class ScriptedGemini(BaseLlm):
    """Supply deterministic responses after ADK has constructed each request."""

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
                "You write the Research Brief": "brief",
                "You are the Research Planner": "planner",
                "You are a Research Worker": "worker",
                "You are the local Worker Evaluator": "worker_evaluator",
                "You are the Research Evaluator": "evaluator",
                "Classify the user's reply": "feedback",
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

    def standard_run(self, *tasks):
        self.model.add("brief", brief())
        self.model.add("planner", plan(*tasks))
        self.model.add("evaluator", message("Report for review."))

    async def test_brief_waits_for_clarification_then_runs_the_graph(self):
        self.assertIsInstance(self.assistant.agent, Workflow)
        self.model.add("brief", brief(clarification_questions=["Which jurisdiction?"]), brief())
        self.model.add("planner", plan(CONTROL))
        self.model.add("worker", message("A holder has 55% of the votes."))
        self.model.add("worker_evaluator", assessment())
        self.model.add("evaluator", message("Draft conclusions."))

        first = await self.assistant.ask("Investigate Example Holdings.")
        self.assertEqual(first.stages, ["start", "research_brief"])
        self.assertIn("Which jurisdiction?", first.answer)
        self.assertEqual(first.record.status, "needs_clarification")
        self.assertNotIn("planner", self.model._requests)

        second = await self.assistant.ask("United Kingdom.")
        self.assertEqual(second.stages, [
            "start", "research_brief", "research_planner", "research_workers", "research_evaluator"
        ])
        self.assertEqual(second.record.status, "awaiting_review")
        self.assertEqual(second.record.brief.aliases, ["Example Group"])
        self.assertIn("previous_brief", str(self.model._requests["brief"][1].contents))
        self.assertIn("Example Retail", str(self.model._requests["planner"][0].contents))
        self.assertIn("Checked the original", str(self.model._requests["evaluator"][0].contents))

    async def test_each_worker_has_an_independent_evaluation_loop_and_report(self):
        self.standard_run(CONTROL)
        self.model.add("worker", message("Initial filing."), message("New filing."), message("Registry."))
        self.model.add(
            "worker_evaluator",
            assessment("follow_up", "First provisional report.", "Read later filing."),
            assessment("follow_up", "Second provisional report.", "Cross-check registry."),
            assessment("follow_up", "Material partial finding.", "Look for third source."),
        )
        turn = await self.assistant.ask("Check voting control.")
        report = turn.record.worker_reports[0]
        self.assertEqual((report.status, report.passes), ("partial", 3))
        self.assertIn("follow-up limit", report.stopping_reason)
        self.assertIn("Read later filing", str(self.model._requests["worker"][1].contents))
        self.assertIn("Material partial finding", str(self.model._requests["evaluator"][0].contents))

    async def test_parallel_worker_reports_keep_their_task_and_stop_independently(self):
        self.standard_run(CONTROL, LEGAL)
        self.model.add("worker", message("Ownership finding."), message("Legal finding."))
        self.model.add("worker_evaluator", assessment(), assessment("unresolved", "Gap."))
        turn = await self.assistant.ask("Investigate the two workstreams.")
        by_question = {report.task.question: report for report in turn.record.worker_reports}
        self.assertEqual(set(by_question), {CONTROL.question, LEGAL.question})
        self.assertCountEqual([report.status for report in by_question.values()], ["complete", "unresolved"])
        self.assertEqual(len(self.model._requests["worker_evaluator"]), 2)

    async def test_restart_revision_and_approval_follow_report_review(self):
        self.standard_run()
        self.model.add("feedback", structured(Feedback(action="revise")))
        self.model.add("evaluator", message("Revised report."))
        first = await self.assistant.ask("Screen Example Holdings.")
        second = await self.assistant.ask("Shorten the draft.")
        self.assertEqual(second.stages, ["start", "research_evaluator"])
        self.assertEqual(second.record.draft, "Revised report.")
        self.assertEqual(first.record.draft, "Report for review.")
        saved_brief = await self.assistant.ask("/brief")
        self.assertEqual(saved_brief.stages, ["start", "show_brief"])
        self.assertIn("Example Retail", saved_brief.answer)
        third = await self.assistant.ask("/approve")
        self.assertEqual(third.record.status, "approved")
        self.model.add("brief", brief(brief_only=True))
        fourth = await self.assistant.ask("/restart")
        self.assertEqual(fourth.stages, ["start", "research_brief"])
        self.assertEqual(fourth.record.status, "brief_ready")
        self.assertEqual(fourth.record.request, "Screen Example Holdings.")
        self.assertFalse(fourth.record.draft)

    async def test_all_research_stages_can_load_skills_and_read_the_web(self):
        self.model.add(
            "brief", call("list_skills"), call("load_skill", skill_name="dd-checklist"), brief(),
        )
        self.model.add(
            "planner",
            call("load_skill", skill_name="initiating-coverage"),
            call("load_skill_resource", skill_name="initiating-coverage",
                 file_path="assets/quality-checklist.md"),
            plan(CONTROL),
        )
        self.model.add("worker", call("fetch_public_document", url=URL), message("55% of votes."))
        self.model.add("worker_evaluator", read_call(), assessment())
        self.model.add("url", message("55% of votes.", custom_metadata=retrieval()))
        self.model.add("evaluator", message("The filing supports the finding."))

        with patch("due_diligence.runtime.fetch_document", new_callable=AsyncMock) as fetch:
            fetch.return_value = {"status": "retrieved", "text": "55% of votes.", "url": URL}
            turn = await self.assistant.ask("Investigate the ownership filing.")
        self.assertCountEqual(turn.calls, [
            "list_skills", "load_skill", "load_skill", "load_skill_resource",
            "fetch_public_document", "httpx_fetch", "read_public_url", "url_context",
        ])
        expected = {
            "search_public_web", "read_public_url", "fetch_public_document",
            "list_skills", "load_skill", "load_skill_resource",
        }
        for role in ("brief", "planner", "worker", "worker_evaluator", "evaluator"):
            declarations = {
                decl.name for tool in self.model._requests[role][0].config.tools or []
                for decl in tool.function_declarations or []
            }
            self.assertTrue(expected.issubset(declarations), role)
            self.assertNotIn("run_skill_script", declarations, role)
        self.assertIn("deep-research", str(self.model._requests["brief"][1].contents))
        self.assertIn("Due Diligence Checklist", str(self.model._requests["brief"][2].contents))
        self.assertIn("Pre-Delivery Final Check", str(self.model._requests["planner"][2].contents))

    async def test_grounded_search_text_stays_outside_question_reports(self):
        self.model.add(
            "brief", call("search_public_web", query="Example Holdings official filings"), brief(),
        )
        self.model.add("planner", plan())
        self.model.add("evaluator", message("Discovery is available separately."))
        self.model.add("search", message(
            LEAD, grounding_metadata=types.GroundingMetadata(
                search_entry_point=types.SearchEntryPoint(rendered_content="<div>Search</div>")
            )
        ))
        turn = await self.assistant.ask("Find public leads.")
        self.assertCountEqual(turn.calls, ["search_public_web", "google_search"])
        self.assertEqual(turn.searches[0].text, LEAD)
        for role in ("brief", "planner", "evaluator"):
            self.assertNotIn(LEAD, str([request.contents for request in self.model._requests[role]]))

    async def test_final_evaluator_can_use_reserved_source_budget(self):
        limited = ResearchAssistant(self.model, max_source_calls=2)
        self.addAsyncCleanup(limited.close)
        self.model.add("brief", read_call(), brief())
        self.model.add("planner", plan(CONTROL))
        self.model.add("worker", read_call(), message("The worker could not fetch a source."))
        self.model.add("worker_evaluator", assessment("follow_up", next_step="Read an update."))
        self.model.add("evaluator", read_call(), message("Report for review."))
        self.model.add("url", message("Unverified."), message("55%.", custom_metadata=retrieval()))

        turn = await limited.ask("Investigate voting control.")
        self.assertEqual(turn.source_calls, 2)
        self.assertEqual(turn.record.worker_reports[0].status, "partial")
        worker_inputs = str(self.model._requests["worker"][1].contents)
        self.assertIn("source_call_limit", worker_inputs)

    async def test_url_context_does_not_release_content_without_requested_url_success(self):
        self.standard_run(CONTROL)
        self.model.add("worker", read_call(), message("The source is unavailable."))
        self.model.add("url", message(
            "Unverified synthetic content.", custom_metadata=retrieval("https://other.example.org/")
        ))
        self.model.add("worker_evaluator", assessment("unresolved"))
        await self.assistant.ask("Read the ownership source.")
        response = str(self.model._requests["worker"][-1].contents)
        self.assertIn("unverified", response)
        self.assertNotIn("Unverified synthetic content.", response)

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


class UrlTests(TestCase):
    def test_public_urls_reject_private_addresses_and_credentials(self):
        for url in ("file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://[::1]/",
                    "http://10.0.0.1/", "https://example.com.internal/",
                    "https://user:password@example.com/", "https://example.com:8080/"):
            with self.subTest(url=url):
                self.assertIsNone(public_url(url))
        self.assertEqual(public_url(f"{URL}#page=4"), URL)
