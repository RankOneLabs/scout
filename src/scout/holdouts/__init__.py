"""The holdout lifecycle: export a pending population, release it later.

Sampling lives at the relevance boundary in `scout.scanning.pipeline`; the
storage and claim primitives live in `scout.storage.holdouts`. This package
is the two operator-facing halves that run on them.
"""
