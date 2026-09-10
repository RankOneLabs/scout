#!/bin/bash
# True-local sweeps on frink, second chain: qwen3 Q4 + llama3.3-70b Q4 (Q8 did not fit beside the embed model).
IDC=$(cat ~/apps/identity-config-local.json)
export IDC
until curl -s http://frink:11434/api/tags | grep -q "llama3.3:70b-instruct-q4_K_M"; do sleep 30; done
echo "MODEL READY $(date -u +%FT%TZ)"
for spec in "ops topical" "evals topical" "ops current" "evals current"; do
  set -- $spec; p=$1; v=$2
  args="--name relevance-frink-agent-$p-$v --task-config /tmp/relevance-task-af08aa7b-agent-$p.json --sweep-file /tmp/relevance-frink2-$p-$v.yaml --pricing-catalog /tmp/pricing-local-20260909.json --dossier-root /srv/content-agn"
  sha=$(docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" -e OLLAMA_HOST=http://frink:11434 engagement-scout uv run --no-sync scout feedback batch-replay $args 2>/dev/null | grep "canonical plan sha256" | grep -oE "[0-9a-f]{64}")
  echo "PLAN $p $v sha=$sha $(date -u +%FT%TZ)"
  [ -z "$sha" ] && { echo "NO SHA for $p $v"; continue; }
  docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" -e OLLAMA_HOST=http://frink:11434 engagement-scout uv run --no-sync scout feedback batch-replay $args --authorize-plan-sha256 $sha --execute-paid-replay 2>&1 | grep -vE "ERROR\]|Traceback|File |\^|raise |ImportError|embedding|feedback_result_id|return await"
  echo "EXIT_${p}_${v}=${PIPESTATUS[0]} $(date -u +%FT%TZ)"
done
echo "ALLDONE"
