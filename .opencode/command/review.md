---
description: Evidence-based review of a file, module or feature
---

Review `$ARGUMENTS` for real, reproducible defects — not style or opinion.
Follow this process in order; do not skip steps.

1. **RUN IT.** Do not review statically if there is any way to execute the code.
   Build/render/generate the actual output. If the target produces something
   visual (PPTX, images, PDF), render it to an inspectable format and **look at
   it** before making any claim about it. If execution or rendering is
   impossible here, say so now and mark everything `UNVERIFIED — static read
   only`.

2. **CROSS-CHECK AGAINST THE CODE'S OWN INVARIANTS.** Find what the code already
   asserts, comments, or validates about itself — `LAYOUT_NOTES`,
   `_audit_layout`, `_render_blocking_problems`, `guard_plan`, `verify.json`
   gates, docstrings claiming "X should never happen" — and verify those claims
   against real output rather than against the code's own logic. Re-run the
   project's own audit tooling.

3. **ROOT CAUSE WITH NUMBERS.** For every suspected defect, compute the exact
   values involved (coordinates, sizes, counts, timings) and show the arithmetic
   tying them to a specific line. Not "this might be causing it" but "here is
   the arithmetic proving this line produces this number."

4. **DISPROVE BEFORE REPORTING.** Try to kill each finding: a second input, a
   second setting, a second resolution. If it does not reproduce, say so and drop
   it. Do not report suspicions as bugs.

5. **RANK BY EVIDENCE STRENGTH.** Confirmed-with-exact-cause first, then
   plausible-but-unverified, then cosmetic. Never bury a confirmed bug under ten
   style nitpicks.

6. **MINIMAL FIX, AND WHY THE OLD LOGIC WAS WRONG.** Show the smallest change
   that fixes it, and explain the defect at the level of the wrong comparison —
   "this compared one character where a clause boundary was required" — not
   "add a check here".

7. **CHECK FOR REGRESSIONS OF PAST DEFECTS.** `docs/quality_review.md` and LLD
   §20/§23 are the baseline. If the change reintroduces or half-fixes something
   already recorded there, that is itself a finding.

## Scope discipline

Use the reviewer whose evidence matches the question. Do not ask a
cross-artifact reviewer to judge pixels, or a doc auditor to judge whether code
works. `AGENTS.md` has the routing table.

## Repo traps

Read `AGENTS.md` first. In short: two renderers that have silently diverged
three times; measurements that were once printed as configuration constants;
ordering bugs where a repair pass ran after the pass that invalidated it; and a
legacy `doc-to-video-tutor` entry point with no quality gates at all.

## When the target is source code

Steps 1-7 apply unchanged, plus these. `tester` proves behaviour; these are the
things a behaviour test cannot see.

8. **DUPLICATION — count it, don't assert it.** If two blocks are near-identical,
   give both line ranges and the actual overlap. Then check whether they are
   *deliberately* divergent before filing: `pptx.py` and `slides.py` are two
   layout implementations and extraction is not automatically right — a shared
   helper that grows a `for_deck=` flag is worse than two functions.
9. **DEAD MEANS UNREACHABLE, not unfamiliar.** A symbol with no in-repo caller
   may be a console entry point, a prompt string, or a documented extension
   point. Two live examples: `doc_to_video_tutor/__init__.py` looks like a dead
   shim but is the `doc-to-video-tutor` console script, and `main`/`ask_llm`
   exist twice on purpose mid-migration. Prove unreachability first.
10. **COMPLEXITY — name the function, its length, its branches.** A finding
    without numbers is an opinion.
11. **COMMENTS THAT CONTRADICT THE CODE.** These become LLD defects, because the
    design contract is generated from these decisions. Check that each claim in a
    docstring still holds.
12. **MUTABLE MODULE STATE as a collector** (`LAYOUT_NOTES`, `LOUDNESS_MEASURED`)
    — correct only if every entry point clears it. An uncleared collector is a
    cross-run leak.
13. **QUANTIFY THE PAYOFF.** Lines removed, branches cut, callers unified. A
    "cleanup" saving 4 lines by adding an indirection should be reported as *not
    worth doing* — that is a useful finding too.

Prefer `/review` with the code checklist for a quick pass. For a full design
review of source, invoke the `code-reviewer` agent: it is `edit: deny`, so it
cannot quietly fix what it finds, and it is the role that independently verifies
a change made by `mentor`.

## Output

Under 40 lines:

```
SCOPE: <what you read/executed> @ <commit>
VERDICT: PASS | FINDINGS | FAIL

| # | Location | Defect | Evidence (exact numbers) | Minimal fix | Severity |

RULED OUT: <what looked wrong and why it is not>
TOP 3: <ordered>
```

Do not report a finding you have not verified by producing and inspecting real
output. If you cannot execute or render, say so explicitly and mark all findings
`UNVERIFIED — static read only`.
