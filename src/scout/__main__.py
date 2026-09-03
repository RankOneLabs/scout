#!/usr/bin/env python3
"""Scout — monitors Discord, Farcaster, and Bluesky for relevant posts.

Drafts engagement comments using an actor-critic LLM pipeline.

Usage:
    python scout.py              # Single scan, output digest
    python scout.py --continuous # Run on interval
    python scout.py --stats      # Show scan statistics
    python scout.py --debug      # Verbose logging
"""

from __future__ import annotations

from scout.cli.main import main

if __name__ == "__main__":
    main()
