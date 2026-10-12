---
description: 'FAF · Template plumbing is locked: do not edit it in this repository'
applyTo: 'main.py,scripts/**,tests/**,agent.yaml,azure.yaml,api/**,requirements*.txt,.github/agents/**,.github/prompts/**,.github/skills/**'
---

# Locked plumbing

These files come from the Foundry Agent Factory templates and are checked by the contract tests. Changing them here breaks
the next regeneration and the PR gate. If something in them is wrong, report it in the Foundry Agent Factory repository
(or run `faf-agent-issues-fixer` there) instead of patching the generated copy. Content files (skills, instructions,
evaluation datasets) are the ones you change here.
