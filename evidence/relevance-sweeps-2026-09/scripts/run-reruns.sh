#!/bin/bash
# Reruns for rate-limited variants; preview each to obtain the plan sha, then execute.
IDC=$(cat ~/apps/identity-config.json)
for spec in "ops current" "evals current" "ops topical" "evals topical"; do
  set -- $spec; p=$1; v=$2
  base="docker exec -e SCOUT_MODEL_IDENTITY_CONFIG=$IDC engagement-scout uv run --no-sync scout feedback batch-replay --name relevance-rerun-agent-$p-$v --task-config /tmp/relevance-task-af08aa7b-agent-$p.json --sweep-file /tmp/relevance-rerun-$p-$v.yaml --pricing-catalog /tmp/pricing-openrouter-20260909b.json --dossier-root /srv/content-agn"
  sha=$(docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" engagement-scout uv run --no-sync scout feedback batch-replay --name relevance-rerun-agent-$p-$v --task-config /tmp/relevance-task-af08aa7b-agent-$p.json --sweep-file /tmp/relevance-rerun-$p-$v.yaml --pricing-catalog /tmp/pricing-openrouter-20260909b.json --dossier-root /srv/content-agn 2>/dev/null | grep "canonical plan sha256" | grep -oE "[0-9a-f]{64}")
  echo "PLAN $p $v sha=$sha"
  [ -z "$sha" ] && { echo "NO SHA for $p $v"; continue; }
  docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" engagement-scout uv run --no-sync scout feedback batch-replay --name relevance-rerun-agent-$p-$v --task-config /tmp/relevance-task-af08aa7b-agent-$p.json --sweep-file /tmp/relevance-rerun-$p-$v.yaml --pricing-catalog /tmp/pricing-openrouter-20260909b.json --dossier-root /srv/content-agn --authorize-plan-sha256 $sha --execute-paid-replay 2>&1 | grep -vE "ERROR\]|Traceback|File |\^|raise |ImportError|embedding|feedback_result_id|return await"
  echo "EXIT_${p}_${v}=${PIPESTATUS[0]}"
done
echo "ALLDONE"
