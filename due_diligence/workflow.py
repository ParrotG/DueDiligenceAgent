"""Route investigation turns through an ADK graph with bounded research loops."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Literal

from google.adk import Context, Event, Workflow
from google.adk.workflow import node
from pydantic import BaseModel, Field

from .agents import make_agents


class Plan(BaseModel):
    action: Literal[
        "clarify", "entity", "overview", "propose", "investigate", "review",
        "draft", "revise", "answer",
    ]
    objective: str
    topics: list[str] = Field(default_factory=list)
    message: str = ""
    needs_approval: bool = False
    uses_pending_plan: bool = False
    review_findings: bool = False


class Gaps(BaseModel):
    follow_up: list[str] = Field(default_factory=list)


@dataclass
class Investigation:
    request: str
    history: list[dict[str, str]]
    pending_plan: Plan | None
    plan: Plan | None = None
    topics: list[str] = field(default_factory=list)
    attempted: set[str] = field(default_factory=set)
    findings: list[dict[str, str]] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)
    rounds: int = 0


def structured(schema, result):
    """Accept the decoded or serialized structured output of an ADK node."""
    if isinstance(result, str):
        return schema.model_validate_json(result)
    return schema.model_validate(result)


def make_workflow(model, search, read_url, current, settings, max_source_calls):
    """Build the graph; only its planner and assessment decisions use routing models."""
    agents = make_agents(model, search, read_url, Plan, Gaps, settings.gemini.thinking_level)
    limits = settings.workflow

    def context(work):
        return {
            "request": work.request,
            "history": work.history,
            "plan": work.plan.model_dump() if work.plan else None,
        }

    @node(rerun_on_resume=True)
    async def plan_turn(ctx: Context):
        turn, work = current()
        turn.stages.append("plan")
        work.plan = structured(Plan, await ctx.run_node(agents["planner"], json.dumps({
            **context(work),
            "pending_plan": work.pending_plan.model_dump() if work.pending_plan else None,
        })))
        if (
            work.plan.action == "investigate"
            and work.plan.uses_pending_plan
            and not work.plan.needs_approval
            and work.pending_plan
        ):
            # Execute exactly the topics that were shown to the user.
            work.plan = work.pending_plan.model_copy(update={
                "action": "investigate",
                "needs_approval": False,
                "uses_pending_plan": True,
                "review_findings": work.plan.review_findings,
            })
            work.pending_plan = None
        work.topics = work.plan.topics[:limits.max_topics] or [work.plan.objective]

    def route_plan():
        _, work = current()
        action = work.plan.action
        if action == "investigate" and work.plan.needs_approval:
            action = "propose"
        return Event(route=action)

    def ask_user():
        turn, work = current()
        turn.stages.append("clarify")
        turn.answer = work.plan.message.strip() or "What entity or research scope should I use?"

    def propose():
        turn, work = current()
        turn.stages.append("propose")
        topics = [topic.strip() for topic in work.topics if topic.strip()]
        work.pending_plan = work.plan.model_copy(update={"topics": topics})
        lines = [work.plan.message.strip() or "Proposed investigation topics", ""]
        lines.extend(f"{index}. {topic}" for index, topic in enumerate(topics, 1))
        lines.extend(["", "Which topics should I investigate? You can approve or revise this plan."])
        turn.answer = "\n".join(lines)

    @node(rerun_on_resume=True)
    async def entity(ctx: Context):
        turn, work = current()
        turn.stages.append("entity")
        results = await ctx.run_node(agents["entity"], json.dumps(context(work)))
        work.findings.append({"question": work.plan.objective, "findings": results or ""})

    @node(rerun_on_resume=True)
    async def overview(ctx: Context):
        turn, work = current()
        turn.stages.append("overview")
        results = await ctx.run_node(agents["overview"], json.dumps(context(work)))
        work.findings.append({"question": work.plan.objective, "findings": results or ""})

    @node(rerun_on_resume=True)
    async def investigate(ctx: Context):
        turn, work = current()
        turn.stages.append("investigate")
        questions = list(dict.fromkeys(question.strip() for question in work.topics if question.strip()))
        work.attempted.update(question.casefold() for question in questions)
        results = await asyncio.gather(*(
            ctx.run_node(agents["topic"], json.dumps({
                "objective": work.plan.objective, "question": question,
                "execution_request": work.request,
            }), use_sub_branch=True)
            for question in questions
        ))
        work.findings.extend(
            {"question": question, "findings": result or ""}
            for question, result in zip(questions, results, strict=True)
        )

    def route_findings():
        _, work = current()
        if work.plan.action == "review" or work.plan.review_findings:
            return Event(route="review")
        return Event(route="compose")

    @node(rerun_on_resume=True)
    async def review(ctx: Context):
        turn, work = current()
        turn.stages.append("review")
        result = await ctx.run_node(agents["review"], json.dumps({
            **context(work), "findings": work.findings, "previous_reviews": work.reviews,
        }))
        work.reviews.append(result or "")

    @node(rerun_on_resume=True)
    async def assess(ctx: Context):
        turn, work = current()
        if (
            work.rounds >= limits.max_follow_up_rounds
            or turn.source_calls >= max_source_calls
        ):
            return Event(route="compose")
        turn.stages.append("assess")
        result = await ctx.run_node(agents["gaps"], json.dumps({
            "objective": work.plan.objective,
            "findings": work.findings,
            "review": work.reviews[-1],
        }))
        gaps = structured(Gaps, result)
        fresh = {}
        for question in gaps.follow_up:
            normalized = question.strip().casefold()
            if normalized and normalized not in work.attempted:
                fresh.setdefault(normalized, question.strip())
        work.topics = list(fresh.values())[:limits.max_topics]
        if work.topics:
            work.rounds += 1
            return Event(route="investigate")
        return Event(route="compose")

    @node(rerun_on_resume=True)
    async def compose(ctx: Context):
        turn, work = current()
        turn.stages.append("compose")
        turn.answer = await ctx.run_node(agents["compose"], json.dumps({
            **context(work), "findings": work.findings, "reviews": work.reviews,
            "search_results_available": bool(turn.searches),
        })) or ""

    return Workflow(name="due_diligence", edges=[
        ("START", plan_turn, route_plan),
        (route_plan, {
            "clarify": ask_user, "propose": propose, "entity": entity, "overview": overview,
            "investigate": investigate, "review": review,
            "draft": compose, "revise": compose, "answer": compose,
        }),
        (entity, compose),
        (overview, compose),
        (investigate, route_findings),
        (route_findings, {"review": review, "compose": compose}),
        (review, assess),
        (assess, {"investigate": investigate, "compose": compose}),
    ])
