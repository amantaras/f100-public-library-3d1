#!/usr/bin/env bash
# Pull the latest successful release's AgentOps evidence into .agentops/ for the local Cockpit.
set -euo pipefail
repo="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
run_id="$(gh run list -R "$repo" --branch main --status success --limit 50 --json databaseId,name \
  --jq 'map(select(.name == "2 · Release: dev → production"))[0].databaseId')"
echo "Latest successful release: https://github.com/$repo/actions/runs/$run_id"
tmp="$(mktemp -d)"
gh run download "$run_id" -R "$repo" -D "$tmp"
mkdir -p .agentops/results/latest ".agentops/results/release-$run_id" .agentops/redteam
cp -R "$tmp"/production-baseline/. .agentops/results/latest/
cp -R "$tmp"/production-baseline/. ".agentops/results/release-$run_id/"
if [ -d "$tmp/dev-evaluation/dev" ]; then mkdir -p ".agentops/results/dev-$run_id" && cp -R "$tmp"/dev-evaluation/dev/. ".agentops/results/dev-$run_id/"; fi
if [ -d "$tmp/agentops-evidence" ]; then cp -R "$tmp"/agentops-evidence/. .agentops/; fi
if [ -f "$tmp/redteam/summary.json" ]; then cp "$tmp/redteam/summary.json" .agentops/redteam/latest.json; fi
rm -rf "$tmp"
echo "Evidence synced into .agentops/ (restart or reload the Cockpit)."
