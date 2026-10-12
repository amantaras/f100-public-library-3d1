---
description: 'FAF · Improve public-library-3d1 from evidence: change skills, instructions or dataset, keep tests green'
agent: faf-agent-guide
argument-hint: 'The problem: a wrong answer, a failing case or a result file'
---

Improve the agent: ${input:request:the problem, with evidence (a wrong answer, a failing case number, a result file)}.
First faf-agent-evaluator reproduces it from the results; then faf-agent-content-author makes the smallest content change on a new branch and runs the contract tests.
Show me the diff summary and wait for my yes before anything is committed.
