# PAA walkthrough captures

The `*.txt` files here are the captured `scout paa` output that
[the three-minute PAA evidence tour](../../paa-reviewer-walkthrough.md)
links to. Every file carries the label **Reference execution** on its first
line, and that label means:

- The output is real. Each step runs the `scout` CLI end to end, and the
  JSON, error text, and exit status are what an operator would see.
- The inputs are checked-in files only: the `inbound_reply_surfacing`
  declaration and the redacted artifacts under `evidence/paa/reference/`.
- The database and evidence root are throwaway directories created for the
  run and discarded afterwards.
- Motion ids, event ids, and timestamps are fixture values, and the
  throwaway evidence root is shown as `<evidence-root>`, so the files are
  byte-reproducible and contain no real path, identity, token, or operator
  name.

None of this is production output, and none of it shows a production
autonomy transition. Scout's checked-in tasks are `shadow` or `disabled`.

Regenerate or check the captures with:

```bash
uv run python scripts/generate_paa_walkthrough_captures.py --write
uv run python scripts/generate_paa_walkthrough_captures.py --check
```

`tests/test_paa_walkthrough_captures.py` runs the same check in CI, so the
files cannot drift from what the generator produces.
