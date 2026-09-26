# Working rules — doc-to-video-tutor

## Reviewing: evidence, not opinion

A finding is something you measured, not something you suspect. Before reporting
a defect you must have produced and inspected real output.

1. **Run it.** Never review statically when the code can be executed. Build,
   render, or generate the actual artifact and look at it. A shape can be
   positioned correctly and still look wrong.
2. **Cross-check the code's own invariants.** Find what the code already asserts
   about itself — `LAYOUT_NOTES`, `_audit_layout`, `_render_blocking_problems`,
   docstrings claiming "X should never happen" — and verify those claims against
   the real output, not against the code's logic.
3. **Root cause with numbers.** Compute the exact values and show the arithmetic
   that ties them to a line. "8.30in wide, matching
   `min(8.92, 12.72) − max(0.42, 0.62)`" is a finding; "there may be an overlap"
   is not.
4. **Disprove before reporting.** Re-check at a second input, setting, or
   resolution. If it does not survive, drop it and say you dropped it. A number
   taken from the middle of a process must have its endpoint stated before it
   becomes a finding.
5. **Rank by evidence strength.** Confirmed-with-exact-cause first, then
   plausible, then cosmetic. Never bury one confirmed bug under ten nitpicks.
6. **Minimal fix, and why the old logic was wrong** — at the level of the wrong
   comparison ("this looked at one character where a clause boundary was
   required"), not "add a check here".
7. **Check for regressions of past defects.** `docs/quality_review.md` and LLD
   §20/§23 are the baseline. Half-fixes are findings.

If you cannot execute or render, say so explicitly and mark every finding
`UNVERIFIED — static read only`.

## Which reviewer to use

Each role may only use the evidence it is scoped to. Do not ask one to do
another's job — that is how a fix lands in one renderer and misses the other.

| role | evidence | use for |
|---|---|---|
| `pipeline-architect` | artifacts vs each other, no render | every build; provenance, drift, timing |
| `graphic-reviewer` | rendered pixels (`soffice`/`pdftoppm`) | anything that only *looks* wrong |
| `tester` | L1 gates + ffprobe/ffmpeg | narration contract, loudness, duration, media |
| `reviewer` | prose claims vs `file:line` | docs accuracy — explicitly **not** whether code works |
| `code-reviewer` | the source itself | duplication, dead paths, complexity |
| `mentor` | all of the above, plus product calls | rulings and trade-offs |

## Repo facts that will bite you

- **Two renderers.** `pptx.py` (the deck) and `slides.py` (the MP4) are separate
  layout implementations. A fix in one has silently missed the other three
  times. Check both, or move the logic to a shared helper in `slides.py`, which
  `pptx.py` already imports from.
- **Measurements, never constants.** The build once printed `LOUDNESS_TARGET`
  under a "loudness" label. A configured value is not a reading; say which one
  you mean.
- **Ordering matters.** Several defects were a pass that ran *after* another pass
  and so saw a stale plan. If a pass mutates state another depends on, order it
  first or re-run the dependent pass.
- **Two console entry points.** `doc-to-video-tutor` is the legacy path and has
  **no quality gates**; everything gated lives behind `doc-to-studio`. Do not
  assume the primary command name runs the checked pipeline.
- **Use `.venv/bin/python`, never bare `python3`.** The system interpreter cannot
  import this package and fails with a misleading
  `ModuleNotFoundError: No module named 'doc_to_video_tutor'`. `python3` is not on
  any agent's allowlist for that reason. `uv run ...` also works, but adds a
  resolution step you do not need for a one-liner.
- **Run the gate before claiming anything.** `uv run ruff check src tests`,
  `uv run mypy src`, `uv run pytest`. Current baseline: 226 tests.
