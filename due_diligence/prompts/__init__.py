"""Load editable agent instructions from Markdown files."""

from functools import cache
from pathlib import Path


@cache
def prompt(name: str) -> str:
    return Path(__file__).with_name(f"{name}.md").read_text(encoding="utf-8").strip()


def research_prompt(name: str) -> str:
    return f"{prompt('sources')}\n\n{prompt(name)}"
