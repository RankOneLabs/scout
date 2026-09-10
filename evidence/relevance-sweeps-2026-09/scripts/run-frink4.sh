#!/bin/bash
# Third chain: gemma4 Q4 with thinking OFF (SCOUT_REPLAY_REASONING=off, ephemeral container patch of scout.completion_limits) plus qwen3 Q4; llama dropped (Q8 did not fit beside the embed model, Q4 wrote prose instead of tool calls at 4.5 tok/s).
IDC=$(cat ~/apps/identity-config-local.json)
export IDC
echo "MODEL READY $(date -u +%FT%TZ)"
echo "MODEL READY $(date -u +%FT%TZ)"
for spec in "ops topical" "evals topical" "ops current" "evals current"; do
  set -- $spec; p=$1; v=$2
  args="--name relevance-frink-agent-$p-$v --task-config /tmp/relevance-task-af08aa7b-agent-$p.json --sweep-file /tmp/relevance-frink3-$p-$v.yaml --pricing-catalog /tmp/pricing-local-20260909.json --dossier-root /srv/content-agn"
  sha=$(docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" -e OLLAMA_HOST=http://frink:11434 -e SCOUT_REPLAY_REASONING=off engagement-scout uv run --no-sync scout feedback batch-replay $args 2>/dev/null | grep "canonical plan sha256" | grep -oE "[0-9a-f]{64}")
  echo "PLAN $p $v sha=$sha $(date -u +%FT%TZ)"
  [ -z "$sha" ] && { echo "NO SHA for $p $v"; continue; }
  docker exec -e SCOUT_MODEL_IDENTITY_CONFIG="$IDC" -e OLLAMA_HOST=http://frink:11434 -e SCOUT_REPLAY_REASONING=off engagement-scout uv run --no-sync scout feedback batch-replay $args --authorize-plan-sha256 $sha --execute-paid-replay 2>&1 | grep -vE "ERROR\]|Traceback|File |\^|raise |ImportError|embedding|feedback_result_id|return await"
  echo "EXIT_${p}_${v}=${PIPESTATUS[0]} $(date -u +%FT%TZ)"
done
echo "ALLDONE"
