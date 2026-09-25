# Concept Field Guide — Mod 03: Regression Gates

> Purpose: understand the *ideas* before you read the spec. This is the "layer 1" explainer
> for `doc/task/03_regression_gates.md` + `doc/design/03_lld_tests.md` — the hardest module.
> Plain words, why step9 chose it, and what to say in an interview.

## The one sentence to memorize

**A framework measures; LLMOps decides — the exit code of `compare` IS the verdict, and the
CI gate is that verdict made binding.**

## The 5 big ideas (everything else is detail)

1. **Baselines + compare** — you freeze a *known-good* snapshot, then diff every new run
   against it.
2. **Metric registry** — every metric is *typed* (direction / kind / tolerance) so the
   verdict is mechanical, not remembered.
3. **Deterministic offline gate** — the gate never calls a live model (D5); the live judge is
   nightly-informational only.
4. **The 11-step precedence** — the engine checks structure BEFORE comparing values, and
   prioritizes FAIL over REVIEW over PASS.
5. **Tolerance is a calculation** — sample-size floors (`1/(n+1)`) and absolute/relative
   units, so "how much is too much" is defined, never vibed.

---

## Concept by concept

### 1. Baseline snapshot & compare — the core loop

- **Source:** task doc `03_regression_gates.md:5-9` (measurement→decision framing; "the exit
  code IS the verdict", green CI badge) + `:19-23` (compare + snapshot deliverables) +
  Verify `:56-58` (`echo $?` → 0..4) — LLD `03_lld_tests.md:229-283` (compare contract) +
  `:198-227` (snapshot) + `:285-299` (gate data flow).

- **Plain words:** run the goldens over the current code → produce a *report* of metric
  values. On a known-good day you save that report as a **baseline** (a committed JSON file).
  Every later change re-runs the suite and **compares** the new "candidate report" against the
  stored baseline. If any metric drifted past its tolerance → regression.
- **Why step9 chose it:** this IS the difference between "we compute scores" and "we operate
  a system". The green CI badge is the artifact interviewers believe.
- **Interview line:** *"The candidate report is diffed against a committed baseline snapshot.
  The result is a verdict (0 PASS / 1 FAIL / 2 REVIEW) — that exit code is literally the CI
  gate's decision."*
- **LLD ref:** `snapshot.py`, `compare.py`; T-03-5/5a (snapshot round-trip & completeness),
  T-03-6/7 (PASS/FAIL).

### 2. The metric registry — metadata that makes verdicts mechanical

- **Source:** task doc `03_regression_gates.md:15-18` (deliverable 1: direction + kind +
  tolerance; ±0.03 judge / ±20% latency) — LLD `03_lld_tests.md:68-90` (`Metric` schema +
  helpers, registry is DATA) + `:92-103` (tolerance policy) + `:109-119` (the 19 registered
  rows).

- **Plain words:** a metric isn't just a number; it carries **kind** (gate = hard fail ·
  guardrail = soft REVIEW · info = recorded only), **direction** (higher-is-better or
  lower-is-better), **tolerance** (how far from baseline counts as a regression), and
  **unit** (absolute delta like ±0.03, or relative fraction like ±20%). The registry is pure
  DATA that drives compare — no logic hidden in it.
- **Why step9 chose it:** if verdict logic is scattered across code, nobody can audit it; a
  typed registry makes "gate = fail, guardrail = review, info = never a verdict" executable
  and reviewable (T-03-1 / T-03-8b/8c enforce it).
- **Interview line:** *"The registry types every metric: kind (gate/guardrail/info), direction
  (higher/lower), tolerance + unit. The verdict engine is just a mechanical reader of that
  metadata."*
- **LLD ref:** `metric_registry.py` contract; T-03-1 (schema valid), T-03-8/8a (REVIEW vs
  FAIL priority), T-03-10 (unknown id → KeyError).

### 3. Kind = verdict semantics (gate vs guardrail vs info)

- **Source:** task doc `03_regression_gates.md:18` ("Gate = hard fail; guardrail = soft
  REVIEW; info = tracked") + gate-policy table `:37-43` + Q3 (`:75-78`) — LLD
  `03_lld_tests.md:78` (kind field) + `:109-119` (rows) + `:256-263` (compare steps 6–11,
  incl. missing-guardrail skip at `:259-261`).

- **Plain words:** these three kinds control what the evidence *means*:
  - **gate**: hard contract — regression = **FAIL**, merge blocked.
  - **guardrail**: soft target (quality/latency that *should* hold) — slip = **REVIEW** (⚠️,
    build still green but flagged), and only if present in BOTH runs.
  - **info**: recorded for provenance, **never** a verdict — can't break a build by existing.
- **Why step9 chose it:** not every slip deserves a red build. Latency slipping 20% shouldn't
  block; correctness regressing should. Kind is the policy layer.
- **Interview line:** *"Gate = hard fail, guardrail = soft REVIEW with a warning, info =
  recorded never verdict. The meaning is in the metadata, and the missing-guardrail rule
  means a not-yet-computed row never keeps the gate permanently yellow."*
- **LLD ref:** T-03-8 (REVIEW), T-03-8b (missing gate → FAIL), T-03-8c (missing guardrail →
  skip), T-03-8a (FAIL beats REVIEW).

### 4. Tolerance and the two units — how much is "too much"?

- **Source:** task doc `03_regression_gates.md:16-18` (±0.03 judge absolute, ±20% latency
  relative, "above measurement noise") — LLD `03_lld_tests.md:80-81` (tolerance +
  tolerance_unit fields) + `:268-269` (direction "worse" formulas) + `:282-283` (inclusive
  band: 0.97 PASS / 0.96 FAIL, verified).

- **Plain words:** absolute (`baseline - 0.03`, e.g. judge metrics) vs relative
  (`baseline * (1 + 0.20)`, e.g. latency ±20%). For "higher is better": worse means
  `value < baseline - tol` (absolute) or `value < baseline*(1−tol)` (relative). For
  "lower is better" it inverts. And the boundary is **inclusive** — value exactly on the
  computed bound still passes.
- **Why step9 chose it:** "+margin conservatively, within one decimal" keeps the gate honest
  to the free-tier's measurement noise (D10); two units exist because a 20-line pure answer
  score and a 300 ms latency are not comparable scales.
- **Interview line:** *"Tolerance is explicit per metric and sized above measurement noise:
  ±0.03 absolute for judge-style scores, ±20% relative for latency. Direction defines
  'worse'; the boundary is inclusive."*
- **LLD ref:** T-03-6a (boundary PASS 0.97), T-03-7 (FAIL 0.96), T-03-9a (relative 120ms
  PASS / 121ms REVIEW), T-03-8f (direction-lower absolute).

### 5. Sample-size floor `1/(n+1)` — a single flip must always trip the gate

- **Source:** **NOT in the task doc — LLD-owned** (D39): `03_lld_tests.md:83`
  (`expected_sample_size` field) + `:89` (`validate_registry` asserts tolerance ==
  1/(expected_sample_size + 1)) + `:97-103` (rationale: S1's silent 2-row hole at n=34).
  Task doc only sets the per-source goal `03_regression_gates.md:51` (≥3 metrics per source).

- **Plain words:** per-source gate rows (retriever / correctness) use a tolerance equal to
  `1/(n+1)` where `n` is the *committed* golden count for that source. One flipped golden row
  must ALWAYS fail that source's gate. This is why the tolerance is stored against the
  registered `expected_sample_size`, not the candidate's own count.
- **Why step9 chose it:** step4's S1 retriever silently needed **2** flips to be seen
  (n=34, ±0.03 → a single flip was invisible). The floor formula closes that real hole.
- **Interview line:** *"Per-source gates use a single-flip floor: tolerance = 1/(n+1) at the
  committed n. One edited golden row always trips that source's gate — no silent two-row
  holes."*
- **LLD ref:** T-03-6b (33/34 vs 34/35 boundary → FAIL), T-03-3 (per-source rows + their n),
  T-03-8i (coverage shrink/growth → FAIL).

### 6. The 11-step precedence — structure before values, FAIL over REVIEW

- **Source:** task doc `03_regression_gates.md:5-7` (registry→compare→verdict pipeline) +
  Q1 (`:63-67`, three layers) — the **11 steps themselves are LLD-only**:
  `03_lld_tests.md:240-264` (numbered block: structure `:241-255`, values `:256-263`,
  precedence `:263-264`) + invariant `:265-267` (no rounding before verdicting).

- **Plain words:** compare first checks the *shape* of things (is the baseline there? is the
  candidate valid? do the sample sizes match the committed n? is every gate metric present?),
  and only then compares *values*. Order wins: a gate failure is never downgraded by a
  guardrail (FAIL > REVIEW > PASS), and a candidate that *drops* a gate metric is treated as
  a regression (FAIL), not ignored.
- **Why step9 chose it:** silent failures are worse than red builds. A suite that stops
  reporting a gate is itself a regression; a shortcut that hides infra errors behind a
  verdict is lying to CI.
- **Interview line:** *"compare runs 11 steps — structural checks first (baseline resolvable,
  candidate valid, coverage matches the registered n, no gate silently dropped), then value
  comparisons, with FAIL > REVIEW > PASS precedence. A gate failure is never downgraded."*
- **LLD ref:** the 11-step block in `compare.py` contract; T-03-8b/8d/8g/8h.

### 7. Structural error classes — exit 3 vs 4 (D36)

- **Source:** task doc `03_regression_gates.md:19-21` (deliverable 2: "3=eval/input error,
  4=config/baseline error (D36)") + exit criterion `:52` + Verify `:58` — LLD
  `03_lld_tests.md:270-273` (error classes raised; argparse native exit 2 must not escape) +
  `:275` (main() exit map 0..4).

- **Plain words:** exit code 0 PASS, 1 FAIL, 2 REVIEW are *verdicts*. 3 = evaluation/input
  error (malformed report, duplicate metric ids, empty goldens) and 4 = config/baseline error
  (missing baseline file, broken `active.json` pointer, zero baseline with relative
  tolerance). Verdicts and infrastructure errors are distinct so CI can display different
  diagnostics ("regression" vs "gate infra" vs "re-baseline").
- **Why step9 chose it:** a broken baseline must be shown as "please re-baseline", never
  confused with a product regression (D35/D36).
- **Interview line:** *"Exit codes are the verdict language: 0/1/2 verdict, 3 eval/input, 4
  config/baseline. CI maps them to distinct diagnostics — regression vs gate-infra vs
  re-baseline."*
- **LLD ref:** T-03-9b (error taxonomy), T-03-5b (pointer battery → all 4).

### 8. `active.json` — the canonical pointer to "the" baseline

- **Source:** **NOT in the task doc — LLD-only** (H12 pointer, H15 Mod-6 switch):
  `03_lld_tests.md:22` (files layout) + `:220-227` (schema `{schema_version, baseline_id,
  path}`, path-safety rules, resolve-BEFORE-compare, atomic rewrite, Mod-6 owns the switch).

- **Plain words:** `eval/baselines/active.json` is a tiny pointer `{schema_version,
  baseline_id, path}` that names which committed baseline is *current*. Compare resolves the
  pointer BEFORE comparing. Any violation (missing file, traversal, id mismatch) → exit 4.
- **Why step9 chose it:** "which baseline?" must be unambiguous; a filename string in CI would
  drift. The pointer is the single source of truth and is swapped atomically alongside new
  baselines (Mod 6 owns the switch).
- **Interview line:** *"`active.json` is a canonical pointer to the current baseline. Compare
  resolves it before diffing; a broken pointer is exit 4, never a fake verdict. The switch is
  owned by Mod 6's promote flow."*
- **LLD ref:** T-03-5b battery, baseline-change policy T-03-13c.

### 9. Deterministic offline gate + nightly live (D5) — the two-lane design

- **Source:** task doc `03_regression_gates.md:5-7` (D5 framing: "enforced by an offline-only
  CI gate") + deliverables 4–5 (`:24-29`: no live API keys / "Never a merge gate") + Q2
  (`:69-73`: determinism, rejected alternative) — LLD `03_lld_tests.md:51-57` (pipeline +
  nightly workflow contract) + `:149-154` (`run_suite`: no network/LLM/env, byte-identity
  rule) + `:301-304` (nightly data flow, "NEVER gated").

- **Plain words:** the **gate** (`llm_eval_gate.yml`) runs `run_suite` — a fully
  deterministic, offline evaluator (no network, no LLM, no env keys; even a *read* of any env
  var is a violation). The **nightly** job (`live_eval_nightly.yml`) uses the real judge on a
  golden subset — informational only, `continue-on-error`, NEVER a merge gate.
- **Why step9 chose it:** a gate that calls live AI will flake (money, latency, nondeterminism)
  and train people to ignore it. Determinism-before-LLM is D5: "gate on live evals" was
  explicitly rejected. The live judge's opinion is data, not a verdict.
- **Interview line:** *"The gate is offline-deterministic — no live model, no env keys, even a
  single env read is a violation. The live judge runs nightly, informational only,
  continue-on-error, never a merge gate (D5)."*
- **LLD ref:** T-03-11 / 11b / 11c / 11d (offline seam — closure scan, env spy, read-only +
  no-socket), T-03-13/13b/13d (workflow contract).

### 10. The offline seam is *enforced*, not asserted (T-03-11 family)

- **Source:** task doc `03_regression_gates.md:27` ("No live API keys in this job") — the
  **three proof mechanisms are LLD-only**: `03_lld_tests.md:165-167` (never touches
  `.env`/`LLM_*`/judge; ANY env read is a violation) + the T-row block `:355-358`
  (T-03-11 closure scan, T-03-11b env spy, T-03-11c read-only+no-socket, T-03-11d deny-set
  coverage).

- **Plain words:** beyond "the code doesn't call APIs", step9 *proves* it three ways: a
  **closure scan** walks the import graph of the eval package and asserts no live-model
  library is reachable (ChatOpenAI, openai, httpx, urllib, socket…); an **env spy** runs the
  suite in a subprocess with env stripped + a shim that rejects any `os.environ` read;
  a **read-only + no-socket backstop** runs the suite in a read-only temp dir with sockets
  blocked. Three independent proof mechanisms.
- **Why step9 chose it:** "I think it's offline" is not a contract; two MEs + one OS-level
  backstop make "gate stays offline" verifiable in CI itself.
- **Interview line:** *"The offline guarantee has three proofs: an import-closure scan
  (deny-set in enforcement list), an env spy rejecting any environment read, and a
  read-only/no-socket subprocess backstop."*
- **LLD ref:** T-03-11, 11b, 11c, 11d.

### 11. The workflow contracts — the gate is its own artifact (T-03-13 family)

- **Source:** task doc `03_regression_gates.md:24-29` (deliverables 4–5: workflow contracts
  live in the task doc) + exit criteria `:47-49` — LLD `03_lld_tests.md:37-50` (always-run,
  no trigger path filter, internal globset no-op, empty `env:`, uv sync, concurrency) +
  `:51-54` (exit mapping: 2 → green + ⚠️ annotation) + `:55-57` (nightly triggers) +
  T-rows `:360-363`.

- **Plain words:** the two YAML files carry explicit contracts: the gate job runs on every PR
  + `workflow_dispatch` (no path filter — a filtered trigger would leave the required check
  Pending forever), uses internal globset detection to no-op when nothing relevant changed
  (with an annotation, still reports pass), empty `env:` (no secrets), uv-synced from the
  lockfile, per-branch concurrency; the nightly job has `schedule` + `workflow_dispatch` only
  and is never a merge gate.
- **Why step9 chose it:** a broken required-check config blocks merging — worse than no gate.
  The "no path filter" rule exists because GitHub skips filtered workflows (leaving the check
  eternally Pending); the internal no-op keeps the check Green while skipping useless runs.
- **Interview line:** *"The gate job is always-run (no trigger path filter — that would strand
  the required check in Pending), internally no-ops when nothing relevant changed, has an
  empty env block, and a per-branch concurrency group. The nightly is schedule-only,
  informational, never a merge gate."*
- **LLD ref:** T-03-13 (nightly source contract), T-03-13b (always-run + outcome mapping),
  T-03-13c (baseline-change policy), T-03-13d (empty env + no-op annotation).

### 12. Baseline-change policy — baselines move deliberately, never silently

- **Source:** task doc `03_regression_gates.md:48` (baseline snapshot committed) — the
  **registered-change rule itself is LLD-only**: `03_lld_tests.md:58-60` (change ships in the
  same commit, noted; broken pointer → exit 4) + `:216-218` (baseline committed to git) +
  `:225-226` (pointer rewritten atomically, H12) + T-03-13c (`:362`).

- **Plain words:** changing `eval/baselines/<id>.json` + `active.json` is a **registered
  change** (D19-analogous): it ships in the same commit, explicitly noted in the PR. The
  pointer is swapped atomically; a broken pointer or incomplete baseline exits 4. A baseline
  that fails "dot baseline regenerated through a bug" is caught, not accepted.
- **Why step9 chose it:** a floor that only passes because someone silently re-baselined away
  the regression is a lie. Movement must be reviewed.
- **Interview line:** *"Baseline swaps are registered changes shipped deliberately — never a
  silent way to make the gate green. A broken pointer exits 4, forcing a re-baseline step
  that's itself audited."*
- **LLD ref:** T-03-13c, `active.json` atomic-rewrite rule.

## Checkpoints (how you know you understood it)

- [ ] Can you explain "exit code IS the verdict" and name all five codes (0..4)?
- [ ] Can you contrast gate vs guardrail vs info with one concrete commit scenario each?
- [ ] Can you explain the single-flip floor and *why* it exists (S1: 34 rows, 2 flips)?
- [ ] Can you state "structure checks before value checks" and give one example?
- [ ] Can you list the 3 offline-seam proofs from memory?
- [ ] Can you answer EVERY Mod-03 Interview-Q&A without notes? (This module has the deepest set.)

## Now read the LLD

`doc/design/03_lld_tests.md` — 39 rows. Mapping: registry → T-03-1/10, run_suite determinism
→ T-03-2..3c/4, snapshot/pointer → T-03-5/5a/5b, compare math → T-03-6..8i/9a, CLI/errors →
T-03-9/9b, offline seam → T-03-11/11b/11c/11d, toolchain → T-03-12, workflow → T-03-13/13b/
13c/13d, end-to-end → T-03-14. When a row still stings, re-read its concept paragraph here
before touching the LLD line.