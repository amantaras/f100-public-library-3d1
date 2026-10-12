---
description: 'FAF · Rules for the files that define what public-library-3d1 knows and how it behaves'
applyTo: 'toolbox-skills/**,.agent_configs/**,evaluations/**,usecase.yaml'
---

# Agent content rules

- The tool names are fixed by `usecase.yaml`: `f100_<id>_<skill>` with `-` replaced by `_`. Never rename a skill without regenerating.
- `.agent_configs/baseline/instructions.md` is the agent's whole persona and routing policy: identity, routing to each tool,
  escalation first, injection resistance, safety rules. Keep every `must_escalate` and `must_refuse` item as a stated behavior.
- A skill's `SKILL.md` is guidance the model reads when the tool is called: concrete, conservative, never inventing facts or numbers.
- Evaluation rows: `ground_truth` is the agent's final reply in its own voice (the judge sees only that text), never a plan such as
  "call the ... tool". `expected_behavior` states only what is visible in that reply. Tool routing belongs in `tool_calls`.
- Indirect injection cases embed a hostile instruction inside quoted content; never use jailbreak markers like "ignore all previous instructions".
- After any change run `python3 -m pytest -q tests`, then evaluate (`/faf-run-evaluation`) before opening a pull request.
- `.agent_configs/baseline/` is also what the Agent Optimizer reads and rewrites: change it by hand or let `/faf-optimize-agent` propose a candidate, never both at once.
