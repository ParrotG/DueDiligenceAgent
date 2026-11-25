"""Construct the agents used by the research workflow."""

from google.adk.agents import Agent

from .config import generation_config
from .prompts import prompt, research_prompt


def make_agents(model, network_tools, skills, schemas, thinking_level):
    """Give each substantive research stage the same public-source tools."""
    roles = {
        "brief": "Research Brief",
        "planner": "Research Planner",
        "worker": "Research Worker",
        "worker_evaluator": "Worker Evaluator",
        "evaluator": "Research Evaluator",
        "report_feedback": "Report Feedback Router",
    }
    agents = {}
    catalogue = f"\n\n{prompt('skill_use')}\n{skills.catalogue()}"
    for role, title in roles.items():
        researches = role != "report_feedback"
        agents[role] = Agent(
            name=role,
            description=title,
            model=model,
            instruction=research_prompt(role) + catalogue if researches else prompt(role),
            tools=[*network_tools, skills.load_research_skill] if researches else [],
            output_schema=schemas.get(role),
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        )
    return agents
