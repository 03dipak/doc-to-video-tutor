# Session scratch (`.temp/`)

The `.temp/` folder is the project's session scratch space. It survives shell/agent
crashes and is independent of the OS `/tmp` (which the OS can purge).

Rule: whenever analysis/probe/QA artifacts are produced during a session, they go
into `.temp/` (or are copied here at the end of the session) so a later session can
pick up where the previous one left off or re-verify a claim.

Layout:

- `.temp/opencode/` — frame extractions, probe scripts (analyze_*.py, scan_*.py,
  pptx_check.py, narr_check.py, quality.py, divider_probe.py, extract*.py),
  render logs (render_v18b.log, ...), gate notes (l1_review.txt), sampled
  reference frames (`ref/`), and reviewer per-variant checks (`rv/`).
- `.temp/smoke/` — the smoke-test render of the progressive-reveal pipeline
  (`smoke.mp4`, generated per-variant slides, throwaway audios).

Exclusions: large regenerable dumps (e.g. `.raw` pixel dumps from scanning) are NOT
kept in `.temp/`; regenerate them with the probe script that produced them.

Deliverables themselves (mp4/pptx/plans) stay in `output/`.