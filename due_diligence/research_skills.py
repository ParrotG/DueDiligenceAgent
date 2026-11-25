"""Expose local ADK-compatible research skills by name."""

from pathlib import Path

from google.adk.skills import load_skill_from_dir

from .config import ROOT


class ResearchSkills:
    """Load project skill metadata and expose textual instructions on demand."""

    def __init__(self, directory: Path = ROOT / "skills"):
        skills = [load_skill_from_dir(file.parent) for file in sorted(directory.glob("*/SKILL.md"))]
        self._skills = {skill.frontmatter.name: skill for skill in skills}
        if len(self._skills) != len(skills):
            raise ValueError("Each research skill must have a unique name.")

    def catalogue(self) -> str:
        """Advertise each skill without adding its body to every model call."""
        return "\n".join(
            f"- {name}: {skill.frontmatter.description}" for name, skill in self._skills.items()
        )

    def load_research_skill(self, name: str) -> dict:
        """Read a research skill from the local catalogue by its exact name."""
        skill = self._skills.get(name)
        if skill is None:
            return {"status": "unknown_skill", "names": sorted(self._skills)}
        return {"status": "loaded", "name": name, "instructions": skill.instructions}
