---
name: faf-agent-lifecycle
description: 'FAF · The development loop of this agent, from content change to production: contract tests, local and DEV smoke, baseline, judged evaluation, Agent Optimizer, pull request gate, release. Use when: how do I improve the agent, what is the next step, how does this repo get to production.'
---

# Agent lifecycle for public-library-3d1

1. **Change content** (`toolbox-skills/`, `.agent_configs/baseline/instructions.md`, `evaluations/`). Never plumbing.
2. **Contract gate**: task *FAF: Contract tests* (`pytest -q tests`). Must be green.
3. **Smoke in DEV**: task *FAF: Ask the deployed agent*. Look at which tools were called, not only the answer.
4. **Baseline**: `scripts/smoke.py --agent public-library-3d1 --dataset evaluations/public-library-3d1-seed-v1.jsonl` asks every dataset case and checks routing.
5. **Judged evaluation**: Foundry judges score every case (relevance, expected behavior, task completion, tool-call accuracy, indirect attack).
   Read `.foundry/results/judged/summary.json`. A low `expected_behavior` score usually means the dataset or the instructions disagree, not that the model is bad.
6. **Optimize**: `/faf-optimize-agent` submits the Foundry Agent Optimizer (preview) against DEV. It creates draft candidate versions and never
   changes live traffic. Only a candidate that clearly beats the baseline is promoted into `.agent_configs/baseline/` for review.
7. **Pull request**: the PR gate deploys an unpromoted candidate and scores it against the DEV baseline; regressions block the merge.
8. **Release**: merging runs the dev release, then production promotion (`/faf-release-agent`).

Which step is next is almost always: the first one that is missing a recorded result under `.foundry/results/`.
