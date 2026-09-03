"""Digest file formatting — pure file-writing / string-building helpers."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from scout.config import RelevanceResult

logger = logging.getLogger("scout.scanning.digest")


def format_result_block(
    r: RelevanceResult,
    draft_comment: str | None = None,
    critique_verdict: str | None = None,
    critique_feedback: str | None = None,
) -> str:
    """Format a single relevant result as a markdown block."""
    lines = [
        f"### Post by @{r.message.author_name} in #{r.message.channel_name}",
        f"> {r.message.content[:500]}",
    ]
    if len(r.message.content) > 500:
        lines.append("> ...")
    lines.append("")
    lines.append(f"**Score:** {r.score:.2f} | **Relevant to:** {', '.join(r.relevant_to)}")
    lines.append(f"**Why:** {r.reason}")
    if r.message.url:
        lines.append(f"**Link:** {r.message.url}")
    lines.append("")
    if draft_comment:
        verdict_label = f" ({critique_verdict})" if critique_verdict else ""
        lines.append(f"**Draft comment{verdict_label}:**")
        lines.append(f"> {draft_comment}")
    if critique_feedback:
        lines.append("")
        lines.append(f"**Critic:** {critique_feedback}")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def write_digest_header(path: str, scan_id: int) -> None:
    """Write the initial digest header (count updated at end)."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    with open(path, "w") as f:
        f.write(f"# Scout Digest — {now}\n")
        f.write(f"**Scan #{scan_id}** | scanning... | 0 relevant posts so far\n\n")


def append_to_digest(path: str, block: str) -> None:
    """Append a result block to the digest file."""
    with open(path, "a") as f:
        f.write(block)


def finalize_digest(
    path: str,
    messages_scanned: int,
    relevant_count: int,
    status: str = "complete",
    overflow_count: int = 0,
    failures: list[dict[str, object]] | None = None,
    gate_blocked_count: int = 0,
) -> str:
    """Update the header with final counts and return full digest text."""
    with open(path) as f:
        content = f.read()

    status_label = f" | status: **{status}**" if status != "complete" else ""
    overflow_label = f" | +{overflow_count} capped" if overflow_count > 0 else ""
    gate_label = f" | {gate_blocked_count} gate-blocked" if gate_blocked_count > 0 else ""
    content = re.sub(
        r"\*\*Scan (#\d+)\*\* \| scanning\.\.\. \| \d+ relevant posts so far",
        lambda m: (
            f"**Scan {m.group(1)}** | {messages_scanned} messages scanned"
            f" | {relevant_count} relevant posts found{gate_label}{status_label}{overflow_label}"
        ),
        content,
        count=1,
    )

    if relevant_count == 0:
        content += "No relevant posts found this scan.\n"

    scan_failures = failures or []
    if scan_failures:
        content += "\n## Scan Failures\n\n"
        for failure in scan_failures:
            platform = failure.get("platform", "?")
            ctx = failure.get("context") or ""
            kind = failure.get("kind", "?")
            msg = failure.get("message") or ""
            http = failure.get("http_status")
            retry_after = failure.get("retry_after")
            retryable = failure.get("retryable", False)
            line = f"- **{platform}**"
            if ctx:
                line += f" ({ctx})"
            line += f": `{kind}`"
            if msg:
                line += f" — {msg}"
            if http:
                line += f" [HTTP {http}]"
            if retry_after:
                line += f" retry_after={retry_after}"
            if retryable:
                line += " _(retryable)_"
            content += line + "\n"
        content += "\n"

    with open(path, "w") as f:
        f.write(content)

    return content
