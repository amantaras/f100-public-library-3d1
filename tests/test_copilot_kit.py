"""The Copilot SDLC kit shipped in this repository is complete and well formed."""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
GITHUB = ROOT / ".github"


def front_matter(path: pathlib.Path) -> dict:
    text = path.read_text()
    block = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert block, f"{path.name} has no front matter"
    fields = {}
    for line in block.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'\"")
    return fields


def agents() -> dict[str, dict]:
    return {p.name.removesuffix(".agent.md"): front_matter(p) for p in (GITHUB / "agents").glob("*.agent.md")}


def test_the_guide_and_the_specialists_exist_and_are_named_faf():
    found = agents()
    assert {"faf-agent-guide", "faf-agent-inspector", "faf-agent-content-author", "faf-agent-evaluator", "faf-agent-optimizer",
            "faf-agent-safety-reviewer", "faf-agent-release-engineer", "faf-agent-issues-fixer"} <= set(found)
    for name, fields in found.items():
        assert name.startswith("faf-") and fields["name"] == name and fields["description"].startswith("FAF · ")
        assert "tools" in fields, f"{name} must declare a tool whitelist"


def test_the_guide_delegates_only_to_agents_that_exist():
    found = agents()
    delegates = re.findall(r"'(faf-[a-z-]+)'", front_matter(GITHUB / "agents" / "faf-agent-guide.agent.md")["agents"])
    assert delegates and all(name in found for name in delegates)


def test_only_the_issues_fixer_may_run_commands_and_every_agent_has_boundaries():
    for path in (GITHUB / "agents").glob("*.agent.md"):
        text = path.read_text()
        assert "## Boundaries" in text, path.name
        tools = front_matter(path)["tools"]
        assert ("execute" in tools) == (path.name == "faf-agent-issues-fixer.agent.md"), path.name


def test_prompts_use_existing_agents_and_the_faf_prefix():
    found = agents()
    prompts = list((GITHUB / "prompts").glob("*.prompt.md"))
    assert len(prompts) >= 8
    for path in prompts:
        fields = front_matter(path)
        assert path.name.startswith("faf-") and fields["description"].startswith("FAF · ") and fields["agent"] in found, path.name


def test_skills_and_instructions_are_present():
    for skill in ("faf-agent-lifecycle", "faf-portal-checks", "faf-domain-skill-author", "faf-agent-instructions", "faf-eval-dataset-author"):
        assert front_matter(GITHUB / "skills" / skill / "SKILL.md")["name"] == skill
    assert (GITHUB / "copilot-instructions.md").is_file()
    for path in (GITHUB / "instructions").glob("*.instructions.md"):
        assert path.name.startswith("faf-") and front_matter(path)["applyTo"]


def test_tasks_exist_and_use_the_agent_of_this_repository():
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text())["tasks"]
    labels = {task["label"] for task in tasks}
    assert {"FAF: Contract tests", "FAF: Ask the deployed agent", "FAF: Run the Agent Optimizer"} <= labels
    assert all(label.startswith("FAF: ") for label in labels)
    assert "{{" not in (ROOT / ".vscode" / "tasks.json").read_text()
