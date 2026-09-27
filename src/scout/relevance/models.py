"""Shared types and routing constants for live Jev relevance."""

# The only projects with a measured v6 relevance catalogue.  Setup, live
# dispatch, and holdout selection all import this single routing authority.
JEV_PROJECT_KEYS = frozenset({"agent-ops", "agent-evals"})


__all__ = ["JEV_PROJECT_KEYS"]
