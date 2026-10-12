"""Enable Foundry-native continuous evaluation for every agent in a project.

Microsoft Foundry has two managed recurring-evaluation mechanisms
(learn.microsoft.com/azure/foundry/observability/how-to/how-to-monitor-agents-dashboard):

* A scheduled evaluation: a Foundry schedule that periodically evaluates the
  agent's latest traces in Application Insights. It works for every agent that
  emits traces, including hosted Invocations agents.
* A continuous evaluation rule (``responseCompleted``): Foundry scores sampled
  live responses. The service accepts rules only for prompt agents; it rejects
  hosted and external agents ("Hosted and external agents are not supported").

This reconciler is idempotent: every agent gets one trace schedule, and every
prompt agent also gets one per-response rule. Both run as the project
managed identity, which needs Foundry User on the project and read access to
the connected Application Insights.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    ContinuousEvaluationRuleAction,
    EvaluationRule,
    EvaluationRuleEventType,
    EvaluationRuleFilter,
    EvaluationScheduleTask,
    HourlyRecurrenceSchedule,
    RecurrenceTrigger,
    Schedule,
)

from azure.identity import DefaultAzureCredential

TRACE_EVALUATORS = ("relevance", "intent_resolution", "task_completion", "task_adherence", "indirect_attack")
RESPONSE_EVALUATORS = ("relevance", "intent_resolution", "task_adherence", "violence", "indirect_attack")
SAFETY = {"violence", "indirect_attack"}


def resource_id(agent: str) -> str:
    """Foundry resource IDs allow letters, digits, hyphens and underscores."""
    return "continuous-eval-" + re.sub(r"[^A-Za-z0-9_-]", "-", agent)


def rule_supported(definition: dict[str, Any]) -> bool:
    """Foundry evaluation rules reject hosted and external agents."""
    return definition.get("kind") == "prompt"


def criteria(names: tuple[str, ...], deployment: str, mapping: dict[str, str] | None) -> list[dict]:
    items = []
    for name in names:
        item = {"type": "azure_ai_evaluator", "name": name, "evaluator_name": f"builtin.{name}",
                "initialization_parameters": {} if name in SAFETY else {"deployment_name": deployment}}
        if mapping:
            item["data_mapping"] = mapping
        items.append(item)
    return items


def ensure_eval(client, name: str, scenario: str, testing_criteria: list[dict], agent: str) -> str:
    for existing in client.evals.list(limit=100):
        if existing.name == name:
            return existing.id
    return client.evals.create(
        name=name, data_source_config={"type": "azure_ai_source", "scenario": scenario},
        testing_criteria=[], extra_body={"testing_criteria": testing_criteria},
        metadata={"purpose": "continuous-evaluation", "agent": agent, "scenario": scenario},
    ).id


def plan(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for agent in agents:
        versions = agent.get("versions") or {}
        definition = (versions.get("latest") or {}).get("definition") or {}
        rows.append({"agent": agent["name"], "kind": definition.get("kind"),
                     "schedule": True, "rule": rule_supported(definition)})
    return rows


def reconcile(endpoint: str, deployment: str, interval_hours: int, max_traces: int,
              max_hourly_runs: int, only: set[str] | None, dry_run: bool) -> list[dict[str, Any]]:
    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True
    ) as project, project.get_openai_client() as client:
        agents = [a.as_dict() for a in project.agents.list()]
        rows = [row for row in plan(agents) if not only or row["agent"] in only]
        if dry_run:
            return rows
        for row in rows:
            agent, rid = row["agent"], resource_id(row["agent"])
            eval_id = ensure_eval(client, f"{rid}-traces", "traces", criteria(
                TRACE_EVALUATORS, deployment, {"query": "{{item.query}}", "response": "{{item.response}}"}), agent)
            run = {"eval_id": eval_id, "name": f"{rid}-scheduled", "data_source": {
                "type": "azure_ai_trace_data_source_preview",
                "trace_source": {"type": "agent_filter", "agent_name": agent, "max_traces": max_traces}}}
            schedule = project.beta.schedules.create_or_update(schedule_id=rid, schedule=Schedule(
                display_name=f"Continuous evaluation: {agent} traces every {interval_hours}h",
                description="Foundry evaluates the agent's latest Application Insights traces on a schedule.",
                enabled=True,
                trigger=RecurrenceTrigger(interval=interval_hours, schedule=HourlyRecurrenceSchedule()),
                task=EvaluationScheduleTask(eval_id=eval_id, eval_run=run),
            ))
            row.update(schedule_id=rid, trace_eval_id=eval_id,
                       schedule_status=str(getattr(schedule, "provisioning_status", "") or ""))
            if row["rule"]:
                response_eval = ensure_eval(client, f"{rid}-responses", "responses",
                                            criteria(RESPONSE_EVALUATORS, deployment, None), agent)
                project.evaluation_rules.create_or_update(id=rid, evaluation_rule=EvaluationRule(
                    display_name=f"Continuous evaluation: {agent} responses",
                    description="Foundry scores sampled live responses for quality and safety.",
                    action=ContinuousEvaluationRuleAction(eval_id=response_eval, max_hourly_runs=max_hourly_runs),
                    event_type=EvaluationRuleEventType.RESPONSE_COMPLETED,
                    filter=EvaluationRuleFilter(agent_name=agent),
                    enabled=True,
                ))
                row.update(rule_id=rid, response_eval_id=response_eval)
        return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--deployment", default="gpt-6-luna", help="Judge model deployment")
    parser.add_argument("--interval-hours", type=int, default=1)
    parser.add_argument("--max-traces", type=int, default=20)
    parser.add_argument("--max-hourly-runs", type=int, default=20)
    parser.add_argument("--agent", action="append", help="Limit to these agents (default: every agent)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows = reconcile(args.endpoint, args.deployment, args.interval_hours, args.max_traces,
                     args.max_hourly_runs, set(args.agent) if args.agent else None, args.dry_run)
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
