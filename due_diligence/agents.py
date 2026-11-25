"""Build the language-model nodes used by the investigation graph."""

from google.adk.agents import Agent

from .config import generation_config
from .prompts import prompt, research_prompt


def make_agents(model, search, read_url, plan_schema, gap_schema, thinking_level):
    """Create independent planner, investigator, reviewer, and synthesis agents."""
    source_tools = [search, read_url]
    return {
        "planner": Agent(
            name="plan_request",
            model=model,
            instruction=prompt("planner"),
            output_schema=plan_schema,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "entity": Agent(
            name="entity_research",
            model=model,
            instruction=research_prompt("entity"),
            tools=source_tools,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "overview": Agent(
            name="preliminary_research",
            model=model,
            instruction=research_prompt("overview"),
            tools=source_tools,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "topic": Agent(
            name="topic_research",
            model=model,
            instruction=research_prompt("topic"),
            tools=source_tools,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "review": Agent(
            name="claim_review",
            model=model,
            instruction=research_prompt("review"),
            tools=source_tools,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "gaps": Agent(
            name="assess_coverage",
            model=model,
            instruction=research_prompt("gaps"),
            output_schema=gap_schema,
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
        "compose": Agent(
            name="compose_response",
            model=model,
            instruction=research_prompt("synthesis"),
            mode="single_turn",
            generate_content_config=generation_config(thinking_level),
        ),
    }
