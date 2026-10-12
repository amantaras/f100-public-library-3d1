---
name: faf-portal-checks
description: 'FAF · Where to look in the Azure and Microsoft Foundry portals to verify what the Foundry Agent Factory created, and what good looks like. Use when: check in the portal, where do I see my agent, traces, role assignment, API Management operations, toolbox, evaluation.'
---

# Portal checks

Give the learner the **place**, the **tab**, and **what good looks like**. Reference points the Factory itself
prints: the **foundry_playground** link on each finished job, and the DEV/PROD project links on the Factory
**Environment** tab. Open the project from there rather than composing URLs.

| To verify | Where | Good looks like |
| --- | --- | --- |
| An agent exists and its version | Foundry portal → your DEV project → **Build → Agents** → the `f100-<id>` agent → **Details** | Status active, the version the job reported, protocols `responses` and `invocations` |
| It answers | Same agent → **Playground** | A completed answer; for a tool question the tool call is visible |
| What it did | Same agent → **Traces** | One trace per request; for an orchestrator, a tool call per specialist asked |
| Evaluation | Same agent → **Evaluation** | The run for this agent version; for judged runs the report link from the Factory catalog |
| Toolbox and skills | Foundry portal → project → **Tools** | `f100-<id>-toolbox` with its skills and the MCP connection |
| API Management operations | Azure portal → API Management → your gateway → **APIs** → `f100-<id>-api` | One operation per synthetic tool; for an orchestrator one per specialist |
| MCP server | Same gateway → **APIs** → `f100-<id>-mcp` → tools | The tools the job's S6 stage verified |
| Specialist access | Azure portal → the Foundry project → the specialist agent → **Access control (IAM)** | **Foundry Agent Consumer** assigned to the API Management gateway's identity, on that agent only |
| Safety attachment | Foundry portal → **Guardrails + controls** | A reviewed content filter attached to the intended deployment (the generated profile alone is a proposal) |
| Logs and telemetry | Azure portal → Application Insights of the platform | Requests and tool spans for the agent name |

Portal layouts change. Say "the tab is named X in the current portal" only when you can see it; otherwise give
the section name and offer to describe what to look for instead.
