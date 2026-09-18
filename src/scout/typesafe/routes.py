"""The single route policy selector for the typesafe shadow node."""

from scout.registry import KeywordRoute

AGENT_OPS_PROJECT_KEY = "agent-ops"


def is_agent_ops_route(route: KeywordRoute | None) -> bool:
    return route is not None and route.project_key == AGENT_OPS_PROJECT_KEY
