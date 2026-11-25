"""Run Brief, Planner, Worker, and Evaluator as an explicit ADK workflow."""

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from google.adk import Context, Event, Workflow
from google.adk.workflow import node
from pydantic import BaseModel, Field

from .agents import make_agents


class Brief(BaseModel):
    subject: str
    purpose: str
    overview: str
    identifiers: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)
    ownership_and_control: list[str] = Field(default_factory=list)
    history: list[str] = Field(default_factory=list)
    notable_features: list[str] = Field(default_factory=list)
    user_priorities: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    candidate_topics: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    brief_only: bool = False


class Task(BaseModel):
    title: str
    question: str
    rationale: str
    success_criteria: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)


class ResearchPlan(BaseModel):
    objective: str
    tasks: list[Task] = Field(default_factory=list)


class Assessment(BaseModel):
    decision: Literal["sufficient", "follow_up", "unresolved"]
    assessment: str
    report: str
    next_step: str = ""


class WorkerReport(BaseModel):
    task: Task
    passes: int
    status: Literal["complete", "partial", "unresolved"]
    assessment: str
    report: str
    stopping_reason: str = ""


class Feedback(BaseModel):
    action: Literal["approve", "revise", "restart"]


class ResearchRecord(BaseModel):
    request: str = ""
    status: Literal[
        "new", "needs_clarification", "brief_ready", "awaiting_review", "approved"
    ] = "new"
    brief: Brief | None = None
    plan: ResearchPlan | None = None
    worker_reports: list[WorkerReport] = Field(default_factory=list)
    draft: str = ""


@dataclass
class Investigation:
    message: str
    history: list[dict[str, str]]
    record: ResearchRecord
    action: str | None = None


def structured(schema, result):
    """Validate an ADK node's decoded or serialized response."""
    if isinstance(result, str):
        return schema.model_validate_json(result)
    return schema.model_validate(result)


def brief_text(brief: Brief) -> str:
    """Show enough of the recorded brief for the user to resolve scope."""
    sections = [f"Research brief: {brief.subject}", brief.overview, f"Purpose: {brief.purpose}"]
    for title, entries in (
        ("Identifiers and codes", brief.identifiers + brief.codes),
        ("Aliases and brands", brief.aliases + brief.brands),
        ("Ownership and control", brief.ownership_and_control),
        ("History", brief.history),
        ("Notable features", brief.notable_features),
        ("User priorities and scope", brief.user_priorities + brief.scope),
        ("Candidate topics", brief.candidate_topics),
        ("Sources", brief.sources),
        ("Questions to confirm", brief.clarification_questions),
    ):
        if entries:
            sections.append(f"{title}\n" + "\n".join(f"- {item}" for item in entries))
    return "\n\n".join(section for section in sections if section)


def make_workflow(model, network_tools, skills, current, settings, worker_source_limit):
    """Create the investigation graph and one bounded local loop per worker."""
    agents = make_agents(
        model, network_tools, skills,
        {"brief": Brief, "planner": ResearchPlan, "worker_evaluator": Assessment,
         "report_feedback": Feedback},
        settings.gemini.thinking_level,
    )
    limits = settings.workflow

    @node(rerun_on_resume=True)
    async def start(ctx: Context):
        turn, work = current()
        turn.stages.append("start")
        record = work.record
        action = work.action
        if action is None and record.draft:
            response = await ctx.run_node(agents["report_feedback"], json.dumps({
                "request": work.message, "subject": record.brief.subject if record.brief else "",
                "research_request": record.request, "draft": record.draft[
                    :limits.history_answer_chars
                ],
            }))
            action = structured(Feedback, response).action
        work.action = action
        if action == "show_brief":
            return Event(route=action)
        if action in {"approve", "revise"}:
            return Event(route=action if record.draft else "no_draft")
        return Event(route="brief")

    @node(rerun_on_resume=True)
    async def research_brief(ctx: Context):
        turn, work = current()
        turn.stages.append("research_brief")
        previous = work.record
        carry_brief = (
            previous.status in {"needs_clarification", "brief_ready"}
            and work.action != "restart"
        )
        request = previous.request if carry_brief or not work.message else work.message
        response = await ctx.run_node(agents["brief"], json.dumps({
            "research_request": request, "latest_user_message": work.message,
            "previous_brief": previous.brief.model_dump() if previous.brief else None,
            "conversation": work.history,
        }))
        work.record = ResearchRecord(request=request, brief=structured(Brief, response))
        brief = work.record.brief
        if brief.clarification_questions or brief.brief_only:
            work.record.status = "needs_clarification" if brief.clarification_questions else "brief_ready"
            turn.answer = brief_text(brief)
            return Event(route="wait")
        return Event(route="plan")

    @node(rerun_on_resume=True)
    async def research_planner(ctx: Context):
        turn, work = current()
        turn.stages.append("research_planner")
        response = await ctx.run_node(agents["planner"], json.dumps({
            "request": work.message, "brief": work.record.brief.model_dump(),
            "task_limit": limits.max_topics,
        }))
        plan = structured(ResearchPlan, response)
        unique = {}
        for task in plan.tasks:
            if question := task.question.strip():
                unique.setdefault(question.casefold(), task)
        work.record.plan = plan.model_copy(update={"tasks": list(unique.values())[:limits.max_topics]})

    @node(rerun_on_resume=True)
    async def work_on_task(ctx: Context, node_input: dict):
        turn, work = current()
        task = Task.model_validate(node_input)
        context = {"task": task.model_dump(), "brief": work.record.brief.model_dump()}
        previous = None
        checks = {task.question.strip().casefold()}
        for number in range(1, limits.max_follow_up_rounds + 2):
            if previous is not None and turn.source_calls >= worker_source_limit:
                return worker_report(task, number - 1, previous, "Worker source budget exhausted.")
            research = await ctx.run_node(agents["worker"], json.dumps({
                **context, "previous_evaluation": previous.model_dump() if previous else None,
            }))
            evaluation = await ctx.run_node(agents["worker_evaluator"], json.dumps({
                **context, "research": research,
                "previous_evaluation": previous.model_dump() if previous else None,
            }))
            previous = structured(Assessment, evaluation)
            if previous.decision != "follow_up":
                return worker_report(task, number, previous)
            check = previous.next_step.strip().casefold()
            if not check or check in checks:
                return worker_report(task, number, previous, "No new follow-up question was available.")
            checks.add(check)
        return worker_report(task, number, previous, "The worker reached its follow-up limit.")

    @node(rerun_on_resume=True)
    async def research_workers(ctx: Context):
        turn, work = current()
        turn.stages.append("research_workers")
        results = await asyncio.gather(*(
            ctx.run_node(work_on_task, task.model_dump(), use_sub_branch=True)
            for task in work.record.plan.tasks
        ))
        work.record.worker_reports = [structured(WorkerReport, result) for result in results]

    @node(rerun_on_resume=True)
    async def research_evaluator(ctx: Context):
        turn, work = current()
        turn.stages.append("research_evaluator")
        record = work.record
        result = await ctx.run_node(agents["evaluator"], json.dumps({
            "request": work.message, "record": record.model_dump(),
            "revision": work.action == "revise", "search_results_available": bool(turn.searches),
        }))
        if result and result.strip():
            record.draft = result
            record.status = "awaiting_review"
            turn.answer = result + (
                "\n\nYou can approve this draft, request a revision, or ask to reopen the Research Brief."
            )
        else:
            turn.answer = "The evaluator returned no draft."

    def show_brief():
        turn, work = current()
        turn.stages.append("show_brief")
        brief = work.record.brief
        turn.answer = brief_text(brief) if brief else "There is no recorded research brief yet."

    def approve_report():
        turn, work = current()
        turn.stages.append("report_approved")
        work.record.status = "approved"
        turn.answer = "Research draft approved."

    def no_draft():
        turn, _ = current()
        turn.answer = "There is no research draft to review. Enter a research request to begin."

    def wait_for_user():
        return None

    return Workflow(name="due_diligence", edges=[
        ("START", start),
        (start, {
            "brief": research_brief, "revise": research_evaluator,
            "approve": approve_report, "no_draft": no_draft, "show_brief": show_brief,
        }),
        (research_brief, {"wait": wait_for_user, "plan": research_planner}),
        (research_planner, research_workers, research_evaluator),
    ])


def worker_report(task, passes, assessment, stopping_reason=""):
    """Keep partial work useful while distinguishing it from a resolved task."""
    status = {"sufficient": "complete", "follow_up": "partial", "unresolved": "unresolved"}
    return WorkerReport(
        task=task, passes=passes, status=status[assessment.decision],
        assessment=assessment.assessment, report=assessment.report, stopping_reason=stopping_reason,
    ).model_dump()
