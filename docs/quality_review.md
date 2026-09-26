# Studio quality review — triage & hardening plan

Context: `doc-to-video-tutor` generates educational videos + PPTX from docs. The
lesson director LLM is **Qwen2.5-7B-Instruct-AWQ** (small, int4, free endpoint),
so prompt-level reliability engineering matters as much as the media code.

External reviews (Perplexity + Claude) were triaged by the mentor role. Status
below is live as of this session.

---

## A. Fix now — cheap, real, high-value

| # | Item | Verdict | Status |
|---|------|---------|--------|
| 1 | `ask_llm` sends no `max_tokens`; server cap can truncate the 5–8 scene JSON mid-object → uncaught `RuntimeError` | Confirmed; set explicit generous `max_tokens` | TODO |
| 2 | `assemble_video`: `clip.resized(lambda t, dur=dur: 1 + 0.015*(t/dur))` — div-by-zero if TTS audio has `duration==0` (empty/whitespace narration from degenerate LLM output) | Confirmed; guard `dur = max(dur, 0.1)` | TODO |
| 3 | Sequential TTS: `asyncio.run()` per scene → 6–9 serial edge-tts round-trips | Confirmed; one `asyncio.run(gather(...))`, order preserved | TODO |
| 4 | No `font=` in Pillow `ImageDraw.text` → tiny non-antialiased bitmap font; not "styled cards" (PPTX does set fonts) | Confirmed; load TTF (DejaVu) with per-block sizes, fallback to default | TODO |
| 5 | `render_slide` vertical budget: only diagram/code panels guard `yy`; steps/bullets/flow/takeaways can draw off-canvas silently | Confirmed; shared remaining-budget guard + truncate | TODO |

## B. Fix next — medium, architectural

| # | Item | Verdict | Status |
|---|------|---------|--------|
| 6 | `ask_llm` retry-on-HTTPError is blunt (retries any status; `ConnectionError`/`Timeout` uncaught → mid-generation server hiccup dies with no retry) | Narrow fallback to 4xx param-unknown; catch conn/timeout once with backoff | TODO |
| 7 | Whole-plan regeneration on single-heuristic failure regresses other dimensions (empirically observed: retries re-drifting grounding). Replace with targeted scene patch — rewrite only offending scene fields, keep rest frozen | Correct root-cause fix for the retry spiral; careful re-merge + re-gate | TODO |
| 8 | Drop `repeat_grip` from retry prompt (already removed from retry *trigger*; adding failure phrases back can re-seed the template echo) | Companion to #7 | TODO |

## C. Skipped / low priority (documented)

- moviepy `.close()` leak — once-per-CLI process, not worth churn.
- `_narration_is_pure_english` only needs 3 marker hits lesson-wide — true, but a stricter gate makes the 7B's job harder; keep warn-only.
- `_content_ngrams` line-scoped bigrams under-flag wrapped paragraphs — conservative (safe) direction; leave.
- `_Progress._tick` underscore API — cosmetic rename; skip.
- Final slide trailing silence — intentional hold; add comment when touching render.

## D. Solid (no change)

- `_ppt_bullet` raw-OOXML (correct element ordering + cleanup before insert).
- Layered heuristics: grounding (bigram/token overlap), section-label normalization,
  topic-drift, narration-repeat + degeneracy regeneration.
- Sourcing from source-driven scene frame; grounding gates now PASS for the single-doc
  build (0 ungrounded).
- `render` subcommand: deterministic media from saved plan (iterate plan cheaply,
  encode once).

## E. Acceptance loop (how we ship 100%)

1. Apply A-1..5, then B-6..7 with `uv run mypy src/ && uv run ruff check src/`.
2. Planner checkpoint: `doc-to-studio build <docs> --minutes 4 --out <name> --skip-video`
   → `doc-to-studio review output/<name>.plan.json` must be PASS (soft narration-repeat ok).
3. Deterministic render: `doc-to-studio render output/<name>.plan.json --out <final>`.
4. graphic-reviewer verifies template conformance (accent bar, TAKEAWAY chip,
   footer) + content fidelity vs source docs.
5. Open quality gap after this pass: narration *depth* (transition filler vs
   explaining the concept) — next STUDIO_PROMPT work.

---

## F. Round-2 external review (Claude.ai on v13 plan) — hardening backlog

Status: `lld_tests_final.*` (from v13) kept as current deliverable — user judges
the video clearly better than prior versions. Intermediate runs archived under
`output/_archive/`.

| # | Finding | Verdict | Status |
|---|---------|---------|--------|
| 1 | Opening line literally reused the prompt's "never copy this sentence" example ("yeast kaam kasa karto"). Passed `_opening_is_on_topic` because a later sentence had a topic word. | Real. Add direct n-gram check of generated opening vs STUDIO_PROMPT example strings; reject/retry on any match. | DONE. `_opening_template_hit()` compares opening 2/3-grams to n-grams derived at import from the prompt's own example (DOTALL capture). Wired into `_still_bad`, `_scene_problem_map` (scene-1 fix msg), `guard_plan`/`review`. Verified: v13 review now FAILS with "opening reuses prompt example sentence". |
| 2 | "vary narrations" rule violated in every scene ("ha na, mhanje aapne dekha ki"). `_narration_tail_repeat` still detects+warns, but `_scene_problem_map` NEVER calls it, so the new targeted patcher never rewrites those scenes — regression vs old `repeat_grip` (which we removed per Perplexity). | Real. Wire repeated 3-grams into `_scene_problem_map` (flag every scene containing one) so the patcher rewrites them. Keep warn-only, no whole-plan regen. | DONE. `_scene_problem_map` now calls `_narration_tail_repeat(quiet=True)` and adds a per-scene rewrite fix for every scene containing a repeated phrase. Still warn-only (not in `_still_bad`), no whole-plan regen. |
| 3 | Topic is "metric evaluation" but scenes never touch gates/guardrails/tolerance semantics. `_source_outline` only pulls headings + idish-table rows; metric-tolerance content lives elsewhere → never steered into `outline_block`. Grounding passes because it only checks anchored-not-coverage. | Real. Add coverage gate: fraction of source outline items / high-freq source tokens actually referenced across titles+bullets; grip/retry when a stated topic has ~zero representation. | DONE. `_top_source_terms(content, 24)` + `_topic_coverage_problem()` (need ≥5 or ¼ of top terms referenced in titles/bullets/takeaways). Wired into `_still_bad`, `_scene_problem_map` (all-scenes coverage fix), build warnings, and persisted as `source_top` for `guard_plan`/`review`. v13 passes coverage (12/24). |
| 4 | Scene title "m4 example" — lowercase restated section label, no information (schema explicitly REJECTABLE). | Real. Flag title that equals its section label (normalized) as a plan warning. | DONE. `_placeholder_titles()` normalizes title vs section label (strips `m4 ` module prefix + articles, tolerant of plural 's') and flags restated titles. Wired into `_scene_problem_map` (title-rewrite fix), `guard_plan`/`review`, and build warnings. Verified: v13 scene 4 "m4 example" and scene 5 "m5 takeaways" both flagged; v16 clean; synthetic "m4 example" flagged. SOFT (warn-only, no resample) — patched via title-rewrite when a hard round runs. |
| 5 | Scene-5 bullets ≈ plan takeaways (near-duplicates). `_repeated_bullets` only catches exact lowercase match across `bullets`, never compares bullets ↔ takeaways. | Real. Loosen dedup to token-Jaccard similarity across bullets AND takeaways. | DONE. `_near_dupe_bullets()` (token-Jaccard ≥ 0.78) compares each bullet against other scenes' bullets AND the plan takeaways. Wired into `_scene_problem_map` (rephrase fix), `guard_plan`/`review`, build warnings. Verified: v16 scene-5 bullets that mirror its takeaways all flagged (5), v13's `eval/baselines/<id>.json` copy-variant flagged, distinct phrasing at Jaccard ~0.44 NOT flagged. SOFT (warn-only). |

Also recorded: infras fixes from this session are SHIPPED (max_tokens window-safe,
scene-patch dedupe, TTF fonts, vertical budget, dur guard, concurrent TTS, resilient
`ask_llm`). Next build went from 378s → ~60s plan, TTS 60s → 4s.

F1-F3 round landed + live-verified (see statuses above). Extra hardening surfaced
while testing against the flaky free 7B:

- **review-before-build resample loop** (mentor workflow, now in `build`): after
  `plan_lesson`, the plan is `guard_plan`-reviewed; on hard-gate failures the
  WHOLE plan is resampled, up to 4 samples. Only a passing plan proceeds to media.
  Narration-repeat is the one documented SOFT gate and does NOT drive a resample
  (the 7B revisits it on every regen and it regresses grounding).
- `_patch_opening()` — the targeted scene patcher *cannot* touch the plan-level
  `opening`, so opening fixes route to a dedicated opening-only regeneration; it
  now tolerates plain-text output and UNWRAPS dict-wrapped openings (v16 initially
  stored `{"opening": "..."}` verbatim). A last-chance opening repair runs after
  retries as well.
- `_dedupe_plan_bullets()` — deterministic cleanup drops exact bullet lines reused
  within/across scenes, so copy-paste degeneration (a v14/v15 model-mood failure)
  can't survive regardless of LLM output; runs after the retry/clamp in `plan_lesson`.
- `ask_llm` backs off on 429 (free hubs throttle bursts; 12s/24s/36s retries) and
  bare-rebuild JSON failures raise a clean one-line RuntimeError instead of a
  15-line `requests` traceback.
- v14 was a genuinely poor draw (3 scenes share identical narration, pure-English,
  off-frame) that the review gate now FAILs with per-scene evidence; v16 resampled
  to a clean pass (hard gates green; only soft repeat warning remains). Current
  deliverable: `lld_tests_final.*` rendered from the v16 plan (~181.8s, 720p/24fps).
- OPEN QA FINDING: `assemble_video` requests `bitrate="2500k"` but ffprobe measures
  ~244 kbps video stream — CRF/encoder override wins. Below the graphic-reviewer
  readability threshold (700 kbps @ 720p). Worth fixing with an explicit
  `-b:v`/`-crf` on the encoder before the next "final" render.
  (**RESOLVED in v18**: CBR params `-g 48 -b:v/-minrate/-maxrate/-bufsize 700k -x264-params nal-hrd=cbr` → measured 700,056 bps.)

---

## G. Round-3 cycle — classroom feel (v17) + interview framing (v18)

### G1. Progressive-reveal render (v17, SHIPPED)
QA finding "video is a slideshow, not a lecture" → the visual interaction gap.

- `render_slide(scene,index,total,out,reveal_upto,highlight)`: bullets/takeaways
  reveal one per variant; hidden bullets KEEP their slot so every variant shares
  one layout (headers pixel-identical). Newest bullet = accent dot + accent
  pointer bar + white text; revealed = gold dot.
- `_slide_variants` → intro variant + one per bullet; `render_scenes` returns
  groups; `assemble_video` splits each scene's audio time into intro (15%, cap
  3s) + equal bullet parts, last variant holds the pause, CrossFadeIn(0.6).
- Removed the Ken Burns zoom + padding/crop entirely → static full-frame clips,
  so the L-bar edge artifact is impossible by construction. Encoder: `-crf 19`,
  no `bitrate=` (ffmpeg only adds `-b` when bitrate is not None).
- Deterministic opener/closer variety: `_assign_openers` REPLACES each narration's
  first sentence with a rotated Hinglish opener (pool of 8); `_assign_closers`
  appends rotated rhetorical closers on alternating scenes. Fake-filler repeat
  count dropped 21 → 16.
- QA-verified (tester + graphic-reviewer on `lld_tests_final`): reveal confirmed
  frame-wise (dots ramp 0→1→2→…; takeaways 1→5), no black edges, PSNR 39-44 dB
  vs plan render, deck opens/sync OK. Remaining: narration still thin (178.7s vs
  240s target), takeaways ≈ bullets verbatim (5), 1 fused token, deck 7 slides
  (extra title card), video bitrate low (26kbps — static content, visually clean).
- Scratch convention: analysis/QA artifacts now live in `.temp/` (see
  `docs/scratch.md`); regenerable `.raw` dumps excluded.

### G2. Interview framing (v18, SHIPPED)
User goal reframed by Claude: the lesson must make the learner **interview-ready**
(reconstruct the design reasoning, not recall facts). Adopted cuts (kept to ONE
field + prompt rules to bound the 7B):

- New scene field **`design_decision`**: "X not Y because Z", rendered as a
  top-of-slide "WHY THIS" card (accent bar + gold label) in both video and deck;
  empty string when the docs imply no genuine alternative (rule: never invent).
- `_contrast_sentences()` miner: sentences with `unlike|never|must|not the same
  as|instead of|rather than|as opposed to|compared with|whereas`, paired with
  the nearest heading → fed as a `DESIGN CONTEXT` block into the plan prompt and
  the scene-patch frame. Verified on the LLD docs: yields exactly the
  "informational, NEVER a gate" / "not a merge gate (D5)" reasoning.
- `_repeated_terms()`: bold/backtick'd doc-emphasised terms (≥3 hits) also fed
  into DESIGN CONTEXT.
- `_source_outline` gains a table-row fallback (any plausible first-cell name +
  non-empty second cell, up to 4) when id-like rows are scarce.
- `_patch_scene` schema now includes `design_decision` so patched scenes keep it.
- Prompt RULES added: INTERVIEW FRAMING (mandatory), NARRATION DEPTH (teach the
  reasoning; ~70–110 words/scene toward the minute target), TAKEAWAYS (synthesized
  conclusions, *different* wording from bullets). Matching `decision_grip`,
  `narration_depth_grip`, `takeaway_grip` in the retry rebuild.
- `fused slide token` added to `_SOFT_PREFIXES` (guard-only; not resample-driving).
- Step 1 (media subgoals) landed. Step 2 = build v18 from the LLD docs with the
  free 7B, render, run tester + graphic-reviewer; verify the WHO PUT IT ON SLIDE
  actually uses the mined contrast reasoning and reaches the minute target.

### G2 (final). QA close-out on `output/lld_tests_v18.*` (SHIPPED)

All plan gates green; video/audio specs green; duration back on the 4.0-min target.

| Check | Status | Evidence |
|---|---|---|
| Plan review (`studio review ...v18.plan.json`) | PASS | VERDICT=PASS, exit 0 (was exit 1: repeated narration template phrase) |
| Narration gate | PASS | no repeated ≥3-word phrase across/within narrations; MHE mix, not pure-English |
| Duration vs `--minutes 4.0` | PASS | 240.43 s vs 240 s (+0.18%); static end-hold holds Key Takeaways slide ~231.3→240.4 s |
| Video encode | PASS | 1280×720@24, video 700,056 bps CBR (`-b:v/-minrate/-maxrate/-bufsize 700k + nal-hrd=cbr`), audio 128k mp3, 25.1 MB |
| Progressive reveal incl. "+N more" | PASS | slides reveal 1→5 then the 6th "+4 more…" row renders in the video (marker gap fixed: `_slide_variants` count = `len(_with_overflow(body))`) |
| WHY THIS cards (scenes 1–5) | PASS | design_decision verbatim; gold divider now gold (was accent blue) |
| Scene 6 retitle + deck populated | PASS | takeaway-driven title; deck slide holds the full takeaway list (5/5 exact) |
| Fidelity vs source docs | PASS | all bullet/Why-THIS strings found in `00b/01/02/03_lld_tests.md`; no ungrounded slide text |
| Artifacts | PASS | no white flash / L-bar / freeze / CBR banding (static-frame diffs ≈ 0) |

Deterministic finishing (all lint/mypy-clean, idempotent, LLM-free in render):

- `_clean_narration()` — strips punctuation artifacts left by template removal.
- `_dedupe_narration_templates()` — collapses 7B canned skeletons
  (`_TEMPLATE_AHEM`/`_TEMPLATE_SEEKHTE`) into unique-head reconstructions.
- `_deepen_narrations()` vs the under-length problem: appends each scene's
  design_decision reason (truncated to "X not Y" when a spoken 3-gram would
  collide with a takeaway line, via `_narration_3grams`), reads the final
  takeaways, and reads filterable slide bullets (`_POINTS_LEADS` unique per
  scene; id/`/_*#{}`-ish lines and collision-sharing bullets skipped). Endorsed
  on a fresh 7B draw by design, and safe to run twice.
- Pacing: inter-scene pause default 2.0→3.0 s; end-of-video hold (`--end-hold`,
  default 6.0 s) so the takeaways land.
- Runtime now ~240 s at ~312 narration words (~0.81 wps effective, TTS-paced).

Remaining documented FINDINGs (cosmetic, NON-gate):

1. (RESOLVED) Deck↔video chrome: deck now renders the same accent bar, gold
   divider, separate top-right counter and muted footer as the video, and the
   video opens with a cover card (LESSON + title + opening, TITLE_HOLD=4s) so
   both have exactly 8 segments/slides. Verified 8/8 frames vs 8/8 slides.
2. (RESOLVED) Deck geometry: `_ppt_slide_chrome` + a shared source of truth
   (`_video_blocks()` replicates render_slide's Pillow room() accounting) make
   the deck skip exactly the blocks the video drops — incl. the code panel the
   video rejects on the Example slide — and the scene-5 Why-THIS/code overlap
   is gone (blocks sequential, deepest 6.80in ≤ 7.30in).
3. `_narration_is_pure_english` stays warn-only by design (documented above).

---

## H. Round-4 cycle — narration repeat-proofing + voice layer (v19, SHIPPED)

Scope (see `docs/LLD-narration-voice.md`): kill the last repeated-narration
failures on the mod03 build deterministically, and stop hardcoding the Hinglish
spoken layer. QA on `output/mod03_gates2.*` (tester + graphic-reviewer) — **all
PASS**; 0 repeated phrases; runtime 348.6s → 191.3s.

### Layer A — language-agnostic repeat machinery (token-only, no hardcoded words)

- `_drop_shared_narration_sentences` (cross-scene sentence sweep, ≥3-token key).
- `_enforce_unique_narration_trigrams` — deterministic backstop matching the
  gate's EXACT contract: lowercased tokens, catches within-scene double-speak and
  mixed-case variants (e.g. `The known-good state` vs `the known-good state`),
  settles in ≤4 passes. Runs before AND after `_deepen_narrations`.
- `_drop_repeated_filler` rewritten generic (removed canned/stall/isme regexes).
- `_prune_bullet_takeaway_echo` (jaccard ≥0.78), `_source_design_decisions` used-
  guard, `_deepen_narrations` cross-scene spoken-dd guard + incremental
  `other_grams`, `_DD_LEADS`/`_POINTS_LEADS` 8 entries.
- Prove matrix (English / MHE / Tamil script): repeat gate before→after 11→0,
  15→0, 6→0 — same code, zero words hardcoded.

### Layer B — `NarrationVoice` (narration language = data, not code)

- `@dataclass NarrationVoice`: openers, closers, dd_leads, points_leads,
  narr_heads, skeleton regexes, pure-English markers, takeaways leads.
  Registry `_VOICES = {mhe-mix, english}`; `_make_voice` fails loudly (exit 2).
- Flag is `--narr-voice` (CLI `--voice` stays the TTS voice). Threaded through
  `plan_lesson`/`_assign_openers/_assign_closers/_dedupe_narration_templates/
  _deepen_narrations/_scene_problem_map/guard_plan/review_plan` + `verify`;
  persisted as `plan.json["voice"]`; `verify`/`review`/`render` read it back.
- English voice: `pure_english_legal=True` (gate legal, not inverted/no-op).

### Hardening

- `build --skip-video` resample loop now catches `plan_lesson` RuntimeError
  (LLM flake/degeneration) and resamples instead of crashing; clean exit 1 if
  all 5 samples flaky.

### QA verdicts on `mod03_gates2` (5 scenes, voice mhe-mix)

- `studio verify` : repeats before 0 / after 0, VERDICT PASS.
- `studio review` : VERDICT PASS, exit 0.
- tester (L1+L2): all PASS — 720p@24, ~700kbps video, 128k audio, 191.3s
  (≈49s under 240s target, acceptable; takeaway slides in deck + video).
- graphic-reviewer: all core PASS (template chrome, reveal sync, fidelity,
  MHE narration, real bullets). Minor non-gate findings recorded for a future
  polish round: encoder bitrate 699,991 bps (≈floor), PPTX lacks the video's
  PANEL body cards, deck footer textbox 7.41in (>7.30) + 5 bullets on m2–m4,
  counter denominator 5→6 on final card, stray empty `' '` paragraph per bullet.
### Post-review hardening (Perplexity adoption, 2026-09)

Re-verified after implementing the LLD's Perplexity adoption record §8:

- `studio verify output/mod03_gates2.plan.json` — PASS, repeats 0 before/after,
  protected 0, `design_decision degenerates dropped: 0`, **idempotent: true**,
  zero narration/design-decision deltas vs the shipped plan (true no-op;
  audit written to `output/mod03_gates2.plan_fixed.plan.audit.json`).
- `studio review` — VERDICT PASS, exit 0.
- `uv run ruff check src/` + `uv run mypy src/` — clean (2 files).
- Language matrix (`/tmp/opencode/voice_matrix.py`) — PASS: English/MHE/Tamil
  repairable overlap 3→0 banned; protected terminology survives (reported, not
  banned); verbatim-duplicate scene classified `unsafe` → hard resample gate;
  unknown voice exits 2.
- Latent bugs fixed while verifying: double-period artifacts in deepen (now
  ends with `_clean_narration`), and case-sensitive "already-spoken" guard that
  re-appended bullets on re-verify (compare now lowercased).
- Plan metadata now persisted: `schema_version`, `repeat_policy_version`,
  `narration_voice` (name + sha256 fingerprint + language_tag),
  `protected_trigrams`; atomic tmp→rename JSON writes.

Net: shipped `mod03_gates2` artifacts remain valid (no regen needed); the
repeat policy is now protected-term-aware, sentence-safe, idempotent, and
audited.

## I. Round-5 cycle — hard pre-render gate + deterministic rebuild (v006 fix)

QA found `output/mod03_gates_v006.plan.json` shipped with **4 banned narration
repeats** (`hai kya hai`, `hota hai kya`, `is module se`, `kya hai to`) — the
scenes were too short to trim safely, so the enforcer marked them unsafe, but the
gate listed `repeating narration phrase` under `_SOFT_PREFIXES` and the build
rendered anyway (video render was Ctrl-Z'd at frame 109; mp4 was 48 bytes).

Fix (see LLD §8): banned narration repeats are now a **hard pre-render gate**.

- `_render_blocking_problems(plan, voice)` — re-derived deterministically at the
  render boundary (no LLM, no hardcoded Hinglish list): banned repeats,
  unrepairable scenes, narration integrity, scene count.
- Wired into `build` (before media render) and `render` (first thing): a failing
  plan exits 1 and writes `*.rejected.audit.json` — no TTS/audio/video/PPTX.
- `_repair_unsafe_narrations` + `_rebuild_scene_narration` — scene-level
  deterministic rebuild fallback from title + design_decision + first distinct
  bullet using the voice's words (skips fragments colliding with other scenes),
  wired into `plan_lesson`'s fix chain and `verify`.

### v006 verification

- `studio render output/mod03_gates_v006.plan.json --skip-video` → exit 1,
  `VERDICT: BLOCKED`, `RENDER-BLOCKING: 4 banned narration phrase(s) unresolved`,
  audit written (`*.rejected.audit.json`).
- `studio verify output/mod03_gates_v006.plan.json --out fixed` → 4 banned → 0
  (`deterministically rebuilt narration for 3 unsafe scene(s)`), `VERDICT: PASS`,
  exit 0.
- Rendering the repaired plan passes the pre-render gate and produces slides +
  TTS + PPTX.
- `uv run pytest tests/` 34/34 PASS (new tests: rebuild repairs too-short
  repeating scenes from own fields; render gate blocks banned repeats + out-of-range
  scene count; passes clean plans); `ruff`/`mypy` clean.

## J. Round-6 — mentor rulings on the v012_014 build failure (NOT COMMITTED)

`mod03_gates_v012_014` aborted at the pre-audio gate:
`tts_required_concept_missing: scene:7 narration is transition/title-only with no
teaching content`. Diagnosis below; every item is ruled, not deferred. Nothing in
this section is committed — see the standing instruction at the end.

### J0. Root cause, measured

Scene 7 lost its second bullet to `_drop_ungrounded_slide_text`, leaving 1.
`_has_teaching_claim` then fails on both paths: its bullet fallback requires
`>= 2` bullets, and after the canned opener (`"Isko ek hi example se clear kiya
jaa sakta hai."`) is stripped as fridge-chatter only 6 content words remain
against a floor of 8. **The gate was correct to refuse — the pipeline had
removed the scene's teaching content.** Scenes 2, 5 and 7 all fell to 1 bullet.

### J1. ~~RULING — the ungrounded drop gets a floor, then re-hydrates from source~~ — **SUPERSEDED BY §S3**

> **Superseded.** The floor half was measured and rejected: keeping ungrounded
> bullets to hold the count re-breaks the hard grounding gate this function
> exists to satisfy. Re-hydration from `source_chunk` shipped instead (§S).
> Read §S3, not the text below.

Implement the floor first: `_drop_ungrounded_slide_text` must not take a scene
below 2 bullets. Cheap, deterministic, no LLM call, and it unblocks the build.
Keeping ungrounded text on the slide is the defect the gate exists to prevent, so
the floor is a backstop, not the fix. Follow-up, once the floor is in: re-hydrate
from the scene's existing `source_chunk` (already attached for exactly this
purpose by the thin-repair) before accepting a starved scene. Owner: next build
cycle.

### J2. RULING — stop the line on further builds until J1 lands

Do not spend another 38s + LLM call rebuilding the same lesson. `verify` is
deterministic but cannot invent a bullet the grounding gate correctly removed, so
it will not clear this. Next build is `v012_015` and only after J1.

### J3. SHIPPED (uncommitted) — `_flatten_parentheses` destroyed identifiers

`speech.py` stripped any `digits+colon` preceded by a space:
`'Exit 3: structural error.' -> 'Exit structural error.'`. The guard `(?<!\w)`
tests one character; in `"Exit 3:"` that character is a space, not a word
character, so the assertion passed. The rule targets list markers (`"1. "`) and
must test a **clause boundary**, not a character. Now requires start-of-string or
sentence punctuation. Zero prior test coverage; two regressions pinned, one
through the full `_spoken_variant` path. Would have hit `Phase 2:`, `Rule 5:`,
`T-03-11:` — identifier-heavy text, which this corpus is made of.

Note the digits *were not* the build blocker: fixing them left the finding
intact, because the scene was genuinely starved. Fixed anyway — it is a real
content-destroying bug and it was masking this one.

### J4. RULING — do NOT guard the zero-scene `max_h` crash

`build_pptx` raises `UnboundLocalError` on a plan with zero scenes. Leave it
crashing. A scene-less plan is an upstream invariant violation, and a local
`max_h = 0.0` would convert a loud failure into a silently malformed deck.
Recorded so a later reader does not "fix" it.

### J5. RULING — keep report-not-drop for the slide-10 code panel

The taller diagram pushes a code panel off slide 10 (0.21in short) and the
auditor reports the drop. Acceptable: something must go when a page is full, and
reporting beats silent loss. Current priority is draw-order among OPTIONAL
blocks. If a *narration-referenced* code block is ever dropped, that inverts the
teaching order and the priority rule must change — not yet observed, so not yet
changed.

### J6. RULING — `rg` grant removed from the reviewer allowlist

`rg` is not installed in this environment, so `"rg *": "allow"` is a dead entry
that silently falls through to `"*": "ask"` on every search. Keep `grep`.
Trivial; fold into the next commit.

### J7. BLOCKED ON ENVIRONMENT — graphic-reviewer cannot render

`soffice`, `libreoffice` and `pdftoppm` are all absent; only `ffmpeg`/`ffprobe`
are present. The graphic-reviewer prompt already requires it to declare
"unverified - geometry only" rather than pass a static read off as visual
evidence, so this degrades safely. Installing `libreoffice` + `poppler-utils` is
the unblock. Until then no finding may be attributed to rendered evidence.

### J8. ~~OPEN QUESTIONS RAISED, NOT RULED~~ — **BOTH RULED IN §R2 AND §R3**

> **Superseded.** Coverage was ruled *legitimate, no required floor* (§R2). The
> narration-mismatch question was ruled *re-narrate vs drop* — and that ruling
> was itself retracted when testing showed deletion makes the failure worse
> (§S2). Original questions kept below for the trail.

1. **Coverage as enrichment.** `source section not covered` (soft) now names
   concept sections no scene claimed — 4, 10, 11, 12 on v012_013. Should the
   planner be *required* to cover all numbered concepts, or is partial coverage
   legitimate? Partial is defensible for a `--minutes 4.0` target; a required
   floor would fight the duration budget.
2. **Narration retains dropped text.** Scene 7's narration still speaks
   "Exit four data inconsistency" while the slide no longer shows it. CALL-FLOW
   §8.2 confirmed this defect; J1 fixes the starvation case but not the mismatch.
   The repair is a design decision (re-narrate vs drop the sentence).

### J9. Uncommitted surface, for audit before any commit

`opencode.json` (graphic-reviewer render scope, mentor evidence standard,
reviewer rebuilt for this repo's doc tree — its prior config pointed at a step9
tree with 6 non-existent paths), `config.py`, `plan.py`, `pptx.py`, `validate.py`,
`speech.py`, `tests/test_build_regressions.py`, `docs/CALL-FLOW.md` (new),
this file. **215 tests pass** (212 when written). Later work also touched
`video.py` (measured loudness, §Q1) and `slides.py` (video-path guard, §P);
§Q1 carries the current uncommitted surface.

**Standing instruction: no commit and no push without explicit user permission.**

## K. Open todo list — carried into the next session (state as of end of v012_014 day)

Verbatim carry-forward, with today's effect on each item. Cross-refs in
brackets point at §J above; do not duplicate those rulings here.

- [x] **Defect 2a** — find and document how `source_chunk` is assigned today
  (root cause of the 5/9 misalignment). Done; 8/9 after the fix.
- [x] **Defect 2b** — assign source chunks after scene titles/concepts exist,
  not before. Done.
- [x] **Defect 2c** — required-concept coverage gate / confidence floor so a
  scene cannot ship with an unrelated chunk. Done (`_CHUNK_MIN_CONFIDENCE`).
- [~] **Defect 2d** — provenance digest recorded per assignment (done);
  **make repair read the record instead of the raw string.** Still open. Today's
  coverage check reads `source_assignment`/`section_digest` correctly, but the
  thin-repair / re-hydration path still matches on the raw heading string. This
  is now also the follow-up half of J1.
- [ ] **Defect 4 residual** — scene 4 still resolves to the overview section.
  Needs summary detection, not a weight. Confirmed still true today on
  v012_013 (scene 4 -> "## The 5 big ideas"). Untouched by today's work.
- [ ] **Defect 9 + semantic title gate** — mandatory continuation marker, no
  incomplete-phrase titles. Open. Note the split: takeaway pages *do* emit
  `Key Takeaways (cont. N)`; paginated scene bodies do **not** — the asymmetry
  graphic-reviewer is asked to look for is real.
- [ ] **`visual_pattern` dispatch replacing implicit draw order** — deferred
  until lineage is done. Blocked behind 2d. J5 records why draw order is
  currently acceptable.
- [ ] **Raise `plan.py` coverage.** Re-measured at **54%, 508 missed** (§Q1).
  Original entry kept for the trail: (51%, 492 missed). Last measured before
  today's additions; **re-measure before quoting a number.** Today added
  `_unclaimed_source_sections` (covered by 2 pinned regressions), the
  `source_sections` persist, the `_flatten_parentheses` fix (2 regressions), and
  `_column_balance` (1 non-vacuous two-way regression).
- [ ] **TTS backend seam with capability model.** Open. Still the largest
  unstarted item; nothing today moved it.
- [ ] **Decide with the user: delete the dead top-level `__init__.py`
  (147 stmts).** Open. **Correction (§R5):** measured as 280 lines exposing 20
  public names, not 147 statements, and "dead" was never verified. Deferred.

**Start here tomorrow, in this order:** J1 (drop floor) is the only item gating
a green build — `mod03_gates_v012_014` is still blocked and must not be rebuilt
until it lands (J2). Then re-measure coverage, then 2d, which unblocks both the
repair path and `visual_pattern`.

## L. v012_015 — PASSES where v012_014 blocked (review tomorrow)

Green build: TTS QA PASS, 10 clips, layout clean, -16.0 LUFS / -1.5 dBTP,
504 word timings, 67 VTT cues, 329.4s @ 720p, pipeline 767s.

### L1. The pass is a lucky sample, NOT evidence that J1 is unnecessary

J1 (drop floor) is still unlanded — this was built before it, so it landed
**against** the J2 ruling. Do not read the green result as closure.

Measured bullets per scene: `[3,3,3,3,3,3,3,2,3]`. Scene 8 landed at **exactly
2** — the `_has_teaching_claim` A2.13 floor — after losing "Resolve it before
diffing" to the ungrounded gate. One further drop on scene 8 reproduces the
v012_014 failure identically. v012_014 lost 3 bullets and died; v012_015 lost 1
and survived. The variable is the 7B's sample, not the code. J1 stands.

### L2. Duration trend is worsening — 120% is the worst in the series

v011 77% (true positive) -> v012 102% -> v013 94% -> **v015 120% (4.82 vs
4.00 min, 505 words)**. The `[LONG]` advisory does not block. Two contributing
warnings on one scene: `tts_too_long: scene:2 77 spoken words` and
`tts_long_sentence: scene:2 sentence of 48 words`. Scene-level redistribution
rather than trimming is still the unstarted item from the todo list.

### L3. Concept coverage improved, and the check is not vacuous

v012_015: 9 numbered concepts, 9 sections claimed — 0 unclaimed, correctly.
v012_013 was 8 of 12. Verified by inspecting `source_sections` vs
`source_assignment` directly rather than trusting the zero return, because a
broken check and a clean deck look identical from outside (the same trap
`_column_balance` was pinned against).

### L4. GAP — soft findings never print during `build`

`guard_plan` is only called at `cli.py:312` (verify) and `cli.py:582`. So
`source section not covered` and `narration still speaks about` are **invisible
in build output**; both soft enrichments work only under `verify`. Worth a
decision: either surface soft findings in the build log, or accept that
enrichment signals are verify-only and say so in the LLD. Today they silently
did not appear.

### L5. Confirm — no `.audit.json` on a successful build

v012_015 produced no `*.audit.json` (v012_014's rejection did). Confirm audit is
intentionally rejection-only; if it is meant to be a standing artifact, the
`check_audit_binding` path has nothing to bind on a green build.

## M. Tomorrow's execution plan (ordered, with anchors and done-when)

Line numbers verified against the working tree at end of day. Re-resolve them if
code moves before you start — a stale anchor here is worse than no anchor.

**T0 — emit `*.verify.json` from `build` (§O2).** Leverage, do this first: closes
L4 + L5 at once and is the only durable cut in review cost.

**~~T1 — drop floor (J1).~~ DONE, and the floor half was REJECTED — see §S.**
`plan.py:640`. Do not take a scene below 2 bullets. Currently scenes reached
`[3,3,3,3,3,3,3,2,3]` (v012_015) and `[1,...]` (v012_014, blocked).
Done when: a plan whose only remaining bullet is ungrounded keeps 2, a synthetic
regression reproduces the v012_014 starvation through `build_tts_script` ->
`_has_teaching_claim` (`speech.py:483`) and asserts no `tts_required_concept_missing`,
and v012_014's plan replans green. This is the only task that unblocks a build.

**~~T2 — re-hydrate from `source_chunk`.~~ DONE — see §S.**
`plan.py:640` region. After the floor holds, replace the retained bullet from the
scene's existing `source_chunk` rather than shipping weak grounding. Sequenced
after T1 by design: the floor is the guarantee, re-hydration is the quality.

**T3 — surface soft findings during `build` (L4).**
`cli.py:582` already calls `guard_plan` in one path; the build path does not, so
`source section not covered` (`validate.py:269`, wired at `validate.py:436`,
registered `config.py:273`) and `narration still speaks about` never appear in a
build log. Decide: print them, or document them as verify-only in the LLD.
Done when: either a build log shows the coverage finding, or the LLD says so.

**T4 — remove the dead `rg` grant (J6).** `opencode.json`, `reviewer.permission.bash`.
One line. `rg` is not installed; every search falls through to `"*": "ask"`.

**T5 — install `libreoffice` + `poppler-utils` (J7).** Environment, not code.
Until then graphic-reviewer may not attribute any finding to rendered evidence;
it is currently geometry-only on this box.

**T6 — duration redistribution (L2, todo list).** 120% is the worst in the series
(77 -> 102 -> 94 -> 120). Scene 2 is both `tts_too_long` (77) and
`tts_long_sentence` (48). Scene-level redistribution, not across-the-board trim.

**T7 — re-measure `plan.py` coverage.** Last figure 51% / 492 missed predates
today's additions. Do not quote a number until re-run.

**T8 — confirm audit-on-success (L5).** v012_015 wrote no `*.audit.json`. If
audit is rejection-only by design, say so in the LLD; if it is meant to stand,
`check_audit_binding` has nothing to bind on a green build.

**T9 — product calls still open, need a human.** From §J8: (a) is partial concept
coverage legitimate for a `--minutes 4.0` target, or is there a required floor;
(b) narration still speaks text the slide dropped — re-narrate or drop the
sentence. Neither is an engineering default.

**Verified-clean, do not re-open:** `_flatten_parentheses` clause-boundary fix
(`speech.py:172`), containment slack (`pptx.py:811`, `_contains` at 814),
`_column_balance` (`pptx.py:841`) — the last has a two-way non-vacuous
regression, keep it that way.

**Do not "fix":** the zero-scene `max_h` UnboundLocalError (J4). Deliberate.

## N. Agent review of v012_015 — three agents, cross-checked (mentor triage)

Ran `tester`, `pipeline-architect` and `graphic-reviewer` against
`output/mod03_gates_v012_015`. Not everything below is verified at the same
level — each item says who measured it, and §N5 lists what I did **not**
confirm. Nothing here is committed.

**The agent split earned its keep.** `graphic-reviewer` attempted its render
twice, got `exit 127` both times (no LibreOffice), and downgraded its entire
PPTX report to "unverified - geometry only" while still reporting MP4 findings
backed by frames it actually looked at. That is the honest degradation the
rewritten prompt demands. `tester` refused to claim a result for the network-only
`tts-check` rather than inventing one.

### N1. CONFIRMED BY ME — diagram node clipped off-frame in the shipped video

`slides.py:323`. `w = min((1280-120)//n, 230)`, `pitch = w + 24`,
`right = 60 + (n-1)*pitch + w`. The 230px cap is applied **per node** and ignores
the inter-node gap, so the row outgrows the 1160px content budget:

| nodes | w | pitch | right edge | |
|---|---|---|---|---|
| 4 | 230 | 254 | 1052px | ok |
| **5** | 230 | 254 | 1306px | **clipped 26px** |
| **6** | 193 | 217 | 1338px | **clipped 58px** |
| 7 | 165 | 189 | 1359px | clipped 79px |

`_parse_diagram` on the shipped plan: **scene 9 has 6 nodes** ("Run Value …
Nightly Live (D5)"). The last pill runs off the right edge for that scene's
entire ~30s. Fix must subtract the gaps: `w = min((W - 2*fx - (n-1)*gap)//n, cap)`.

### N2. CONFIRMED BY ME — VTT is ~7s early against the shipped video

First cue `00:00:00.100 --> 00:00:04.968`; last cue ends `00:05:15.001`
(315.0s). Real audio: 7.28s lead-in, clips sum 288.96s, audio ends ~322.3s.
Every caption fires before its audio. `video.py:618` calls `_write_webvtt` on the
un-prepended `timings`; the 4.0s title hold + 3.0s pause are prepended later at
`video.py:637-642`. Independent corroboration: `tester` measured 7.280s via
`silencedetect`. **No subtitle stream is muxed**, so this bites only if the
`.vtt` sidecar ships alongside the video — decide which is the contract.

### N3. CONFIRMED BY ME (pptx side) — deck and video are different renders

`mod03_gates_v012_015.pptx` has **11 slides**, counters `1 / 11 … 11 / 11`.
`graphic-reviewer` counted **12** frames in the MP4 with a `12 / 12` counter,
different block order, and no counter on the title slide. Two renderers
(`pptx.py` python-pptx vs `slides.py` PIL) disagree on structure, so PPTX
geometry review does not describe what a viewer sees. This is the structural
root of N1 surviving: the diagram bug is in `slides.py`, the path the earlier
`_audit_layout` work never covers.

### N4. REPORTED, corroborated, not independently re-measured by me

- **`dropped_slide_text` is a false provenance claim.** `pipeline-architect` #6
  and `tester` W2 agree: scene 8's "Resolve it before diffing" is gone from
  `plan.scenes[7].bullets` but still verbatim in the narration and spoken in
  `scene_08.mp3`. The artifact's record contradicts the artifact. This is
  CALL-FLOW §8.2 confirmed live, and it is the *starvation* cousin of J1.
- **7/9 titles truncated mid-phrase and spoken verbatim** — all three agents.
  `graphic-reviewer` disproved the obvious cause: box inner width is 10.40in and
  the longest title measures 8.98in, so ~0.6in is spare. The cut is a fixed
  ~41-char budget, not a width limit. Scene 3 carries an unclosed bracket
  ("gate vs"). Matches todo item "Defect 9 + semantic title gate".
- **`verify`'s repair chain deletes teaching content** (`tester` M1). Resolving
  one trigram strips the design-decision sentence from scenes 3/7/9 —
  373 → 323 narration words (−13.4%) — while slides 4/8/10 still render that
  "Why THIS" text. Any re-render therefore creates 3 new
  `unspoken visual claim` findings. The shipped media came from the
  *un-repaired* plan, so build and deterministic path disagree.
- **The logged loudness is a constant, not a reading** (`tester` W11).
  `-16.0 LUFS / -1.5 dBTP` are `config.py:20-21` config values. Measured:
  -16.0…-16.6 LUFS, **-1.8 dBTP**. And W7: the MP4's audio sits 3.46 dB below
  the clips, passing C3.2 only because BS.1770 sums identical L/R channels;
  `ffmpeg -ac 1` on the shipped file reads **-19.7 LUFS**, a mono-QC failure.
- **Duration: my §L2 figure was incomplete.** 120% is the *audio* (288.96s =
  4.82 min). The MP4 a viewer watches is 329.38s = **5.49 min = 137%** of the
  4.0 target. `pipeline-architect` corrected my brief on this: 329.4s is the
  container, not the audio timeline. No over-run band exists in `config.py`.
- **Bitrate exactly on target.** 700,124 bps vs the 700 kbps spec — zero margin.

### N5. NOT VERIFIED — do not act on these without checking

1. That `section_digest` is a correct hash of the source **body** (needs source
   text; only internal digest↔section consistency was checked).
2. The build-log claims "narration written by dedicated pass (B)" and "1
   ungrounded drop" — no log artifact ships, and `*.sample1.plan.json` differs
   from the shipped plan by exactly one line, so it cannot audit either.
3. `visual_diagram` flow spans and per-bullet deck↔script coverage.
4. `tester`'s M1 repair-chain numbers were produced against a **copy** in
   `/tmp/opencode/idem/`; worth re-running on the real plan before acting.

### N6. Mentor ruling on ordering

N1, N2 and N3 are all in the **video/`slides.py` path**, which the PPTX auditor
work never touched — that is the lesson of this review, and it outranks any
single fix. Sequence: N3 first (decide which renderer is the contract, or make
them agree), because N1's fix location depends on it. Then N1, then N2. N4's
title item already exists in the todo list; its `dropped_slide_text` half is the
right follow-up to J1 rather than a separate task.

## O. RULING — make review affordable enough to run every build (mentor)

Measured cost of the v012_015 review: **215 toolcalls, 42 minutes**
(pipeline-architect 57c/4m57s, graphic-reviewer 58c/10m30s, tester
100c/26m20s). Per-call cost tracks how much each call re-derives: 5.2s/call for
pipeline-architect, 10.9s for graphic-reviewer, 15.8s for tester. Too expensive to
repeat per build, and that is the real cost — a defect like the §N1 diagram clip
survives precisely because review is a special occasion.

### O0. Diagnosis: the pipeline already knows, and throws it away

`build` computes the TTS audit, the layout audit, loudness and duration, then
prints them as human-formatted stdout that no later process can read. The
findings survive only in `tts_script.audit`; the measurements do not survive at
all. So every reviewer re-derives them: `tester` spent 100 calls reproducing
gates that had already run. **The waste is not agent speed, it is a missing
artifact.**

### O1. DONE NOW — per-agent economy rules in `opencode.json`

Each of the three prompts gained an `ECONOMY` block with a call budget drawn
from the measurement above: pipeline-architect 45, graphic-reviewer 30, tester
25. The three rules that matter:

- **Read the persisted verdict; do not reproduce it.** The build already ran the
  gates. Re-run only on suspicion, and say which gate and why.
- **Probe once.** graphic-reviewer spent 2 calls failing on a missing soffice;
  it must record "render unavailable" and move on, never retry or substitute.
- **Batch by construction.** One call looping all 10 clips beats ten calls
  handling one clip. Manifest is supplied; do not go hunting for the newest plan.

Projected: 42 min -> ~16 min per build, with no loss of coverage.

### O2. NEXT — `*.verify.json`: the real fix, and it closes two open items

Have `build` write one machine-readable evidence file per run holding every
measured value and every gate verdict. That single change:

1. makes the reviewer's job cross-checking rather than re-deriving, which is the
   only durable cost cut;
2. closes **L5** — the missing audit artifact on a successful build;
3. closes **L4** — soft findings (`source section not covered`,
   `narration still speaks about`) become visible in the build output, since the
   file *is* the output.

It also settles **tester W11**: the log printed `-1.5 dBTP` from
`config.py:20-21` — a config constant, not a reading. A verify artifact holding
*measured* values makes that class of dishonesty structurally impossible.

### O3. RULING on which reviews run when

| Tier | What | When | Budget |
|---|---|---|---|
| 0 | `*.verify.json` from the build itself | every build | ~0 (already paid) |
| 1 | `pipeline-architect` cross-artifact | every build | ~4 min |
| 2 | `graphic-reviewer`, `tester` media forensics | on suspicion, before release, or when new code touches `video.py`/`slides.py`/`speech.py` | ~13 min |

Tier 2 is not a per-build gate and must not become one. Its findings in §N were
real and serious, which is exactly why it must stay available — a 42-minute gate
gets skipped, and a skipped gate catches nothing.

**Sequencing note:** §N6 already ruled that §N3 (deck vs video are different
renders) comes first, because the §N1 fix location depends on it. O2 is
independent of that and can be done in parallel; it touches `cli.py`/`video.py`
emit paths, not layout.

**Add as T0 in §M's list** — it is leverage on every future review, and on every
future defect class we have not met yet.

## P. T0b — video-path layout guard (SHIPPED, uncommitted)

§O ruled that the cheapest per-build visual check is not a second agent but a
geometry audit **in the renderer that can actually see the defect**. `_audit_layout`
has guarded the PPTX since 43edaa4; `slides.py` — the PIL path that draws the MP4
a viewer actually watches — had no equivalent. That asymmetry is precisely why
§N1 survived.

**Fix.** `_diagram_row_fit(count)` sizes the row against the whole content band
*including the `count-1` gaps*. The old per-node cap could not: it bounded each
node at 230px and never budgeted for the 24px between them, so the row grew
`pitch = w + 24` per node regardless.

| nodes | old: past frame | new right edge (limit 1220) |
|---|---|---|
| 4 | 0px | 1052px (unchanged) |
| 5 | 26px | 1216px |
| **6** | **58px** | **1218px** |
| 7 | 79px | 1219px |

Four or fewer nodes come out byte-identical to the old cap, so this is not a
visual regression for the diagrams that were already fine.

**Check.** `LAYOUT_NOTES` + a `[WARN] layout:` line from `render_scenes`, same
channel and wording as the PPTX builder, deduped because pagination re-renders
one scene per variant. A defect in this path was previously visible only in the
pixels — which is §L4's failure mode again, in a second place.

**Pinned, both directions** in
`test_diagram_row_cannot_leave_the_frame`:
- `diagram_overflow_px(6) == 58` — the old arithmetic is still reproducible, so
  the test cannot pass by the defect vanishing from the helper;
- the new fit holds for 2..11 nodes, and `_diagram_row_fit(3)[0] == 230` pins
  the unchanged common case;
- the shipped plan is re-parsed and must still contain the 6-node scene, so the
  fixture cannot drift away from the incident.
- Reporting channel proven by reinstating the old `_diagram_row_fit` and
  re-rendering: the build log emits
  `diagram of 6 node(s) puts its last node's right edge at 1338px, 58px past
  the 1280px frame`.

**Note on the reported number.** The warning deliberately reports the loss past
the *frame* (58px), not past the content band (118px). The fit targets the band
because that is stricter, but a band-relative figure overstates the visible loss
by exactly the margin, and `tester` W11 already caught this build printing a
configured constant as if it were a measurement. Same discipline here.

`213 tests pass`, ruff and mypy clean. §N1 is closed; §N3 (pptx and mp4 are
different renders) remains open and is still the larger structural issue.

## Q. Consolidated priority list — all sources merged, with disposition

Merges §K (defect list), §M (T0–T9), §N (three-agent review), §O (review
cost) and §P. One row per item, one owner, one definition of done. Nothing
committed.

### Q1. DONE THIS SESSION

| Item | Result |
|---|---|
| T0b video-path layout guard | §P. Diagram no longer leaves the frame; `[WARN] layout:` now emitted from `render_scenes`; pinned both directions |
| T4 dead `rg` grant | Removed from `reviewer.permission.bash`; `grep` retained |
| T7 re-measure `plan.py` coverage | **54%, 508 missed** (was 51% before today's additions) |
| T0a measured loudness | Build now reports **measured** LUFS/dBTP, not `config.py` constants. Also fixed a latent `os.replace` EXDEV bug that made normalisation skip **every** clip when the output dir is on another device (measured 0/3 → 3/3) |
| `dropped_slide_text` recording + `narration still speaks about` gate | Shipped; confirmed live on v012_013 (2 real references) |
| `_flatten_parentheses` identifier loss | Shipped; `Exit 3:` no longer becomes `Exit` |
| `source section not covered` soft finding | Shipped; 9/9 concepts covered on v012_015, 8/12 on v012_013 |
| agent `ECONOMY` budgets + reviewer rebuilt for this repo | §O1; review 42 min → ~16 min projected |

### Q2. P0 — do next, unblocked

| # | Item | Why P0 | Done when |
|---|---|---|---|
| ~~**P0-1**~~ | **DONE — §S.** Floor measured and rejected; re-hydration shipped. The pre-audio gate now passes on the v012_014 plan that killed a 38s build | `_rehydrate_starved_scenes` + a second thin-repair pass, pinned in `test_starved_scene_is_rehydrated_from_source_before_the_pre_audio_gate` |
| **P0-2** | **§N3 renderer divergence** | Two renderers, two layout guards, two takeaway layouts. A fix reaches one and silently skips the other — that is how §N1 shipped while every audit read clean | **Blocked on a human decision** (see Q4) |
| **P0-3** | **T0b2 `*.verify.json`** | Closes L4 + L5 + W11's remaining half; makes every future review cheap (§O2). T0a is the loudness slice of this | One machine-readable file per build: gate verdicts, soft findings, measured media, plan digest |

### Q3. P1 — after P0

| # | Item | Source |
|---|---|---|
| P1-1 | Video-path takeaways are single-column with no `(cont. N)` — **my `_shorter_column` fix went into `pptx.py` only**, so the empty-right-half defect is still live in the MP4 | graphic-reviewer #3, §N |
| P1-2 | VTT ~7s early against the video; decide whether the sidecar ships with the MP4 at all | §N2, PA #4, tester W8 |
| P1-3 | Titles: 7/9 truncated mid-phrase at a fixed ~41-char budget **and spoken**; scene 3 has an unclosed bracket | all 3 agents; todo "Defect 9" |
| P1-4 | `verify` repair deletes 13.4% of teaching text that slides still display → any re-render creates 3 new `unspoken visual claim` findings | tester M1 (**unverified** — ran on a `/tmp` copy) |
| P1-5 | MP4 audio sits 3.46 dB below the clips; a mono downmix measures −19.7 LUFS and fails C3.2 | tester W7 |
| P1-6 | `1/(n+1)` spoken two different ways in adjacent clips; `±`/`%`/`·` invisible to the audit regex | tester W3 |
| P1-7 | T2 re-hydrate a starved scene from `source_chunk` | §J1 follow-up |
| P1-8 | Defect 2d — make the repair path read `section_digest` instead of the raw heading string | §K |
| P1-9 | T6 duration redistribution (mp4 is 137% of target; audio 120%) | §N17, §L2 |

### Q4. ~~BLOCKED ON A HUMAN DECISION~~ — **ALL FIVE RULED IN §R**

> **Superseded.** Nothing here is blocked. §R1 ruled the renderer consolidation,
> §R2 the coverage floor, §R3 (retracted, §S2) the narration mismatch, §R4
> audit-on-success, §R5 the `__init__.py` deletion. Original list kept below.

1. **§N3: which renderer is the contract?** Options: (a) make `slides.py` consume
   the PPTX geometry so there is one layout; (b) make the PPTX the only artifact
   and render video from it; (c) keep both and accept a shared test suite that
   asserts they agree. This is a consolidation decision, not a patch, and P1-1
   cannot be scoped until it is made.
2. **§J8a: is partial concept coverage legitimate** at `--minutes 4.0`, or is
   there a required floor? A floor fights the duration budget.
3. **§J8b: narration still speaks text the slide dropped** — re-narrate (another
   model call) or drop the sentence (risks the teaching-claim floor).
4. **§L5/T8: is audit rejection-only by design?** If it should stand on a green
   build, `*.verify.json` (P0-3) is where it goes.
5. **§K: delete the dead top-level `__init__.py`** (147 stmts).

### Q5. NOT STARTED, no owner yet

TTS backend seam with capability model (largest unstarted item) · `visual_pattern`
dispatch (blocked behind P1-8) · under-fill / LLD 20.2 #8 · continuation markers
on paginated scene bodies · Pillow/PPTX measurement parity.

### Q6. HOUSEKEEPING DISCLOSURE

While proving T0a, three shipped clips of `mod03_gates_v012_015_audio` were
**re-normalised in place** (scene_01..03) by the test harness. A second pass at
the same target is near-idempotent — measured gain ~0.1 dB, true peak moved from
-1.8 to -1.5/-1.7 dBTP — but those three artifacts are no longer byte-identical
to what the build produced, so any future loudness finding on v012_015 should
re-measure rather than trust the stored files. The MP4 was not touched.

## R. MENTOR CALLS on the five blocked items (§Q4) + the todo list

All five were parked as "needs a human". Taking them, so work is not blocked.
Each states the reasoning so it can be overturned deliberately rather than by
accident.

### R1. §N3 renderer divergence — **PPTX becomes the single layout authority, staged**

Measured first, because it decides feasibility:

- `pptx.py` has **zero** reveal/highlight support; the video needs per-audio-segment
  bullet reveals. That is the one capability the deck lacks.
- The **content** layer is already shared: `pptx.py` imports `_parse_diagram`,
  `_wrap`, `_with_overflow`, `_json_payload_candidates`, `_scene_pages` from
  `slides.py`. Only the **layout/rendering** is forked.

So this is not two products, it is one product with two drawing layers — and the
deck's layer is the mature one (RowStack, height pagination, adaptive cards,
containment slack, column balance, diagram fit). Ruling, in order:

1. Add reveal/highlight variants to `pptx.py`.
2. Render video frames from the PPTX geometry; `slides.py` keeps only the
   ffmpeg/MoviePy assembly and the title card.
3. Delete the drawing code from `slides.py`.

Rejected: (c) "keep both plus an agreement test" — it detects today's drift, not
tomorrow's, and we have three instances of a fix landing in one path only
(`_shorter_column`, `(cont. N)`, the diagram fit). Rejected: big-bang, because
audio alignment and reveal timing work today and must not be risked at once.

**Not this session.** It is multi-session work and independent of P0-1. Until it
lands, P1-1 (video takeaways single-column) is knowingly unfixed, and both layout
guards must be read as covering *different products*.

### R2. Partial concept coverage — **legitimate; no required floor**

A floor fights `--minutes`, and forcing coverage produces shallow scenes — which
then fail the teaching-claim gate. That is the v012_014 failure mode with extra
steps. The soft finding is the correct instrument: it names the gaps, a human or
the planner decides. **No code change.** Document in the LLD.

### R3. Narration still speaks dropped text — **drop the sentence, do not re-narrate**

Deciding factor: the two options fail differently. A re-narrate costs another
model call and can fail silently and variably. A dropped sentence risks the
teaching-claim floor — but that floor is *checked* at the pre-audio gate, so the
failure is deterministic and loud. Checkable beats stochastic.

It is also strictly an improvement in grounding: the text was dropped *because*
it had no source anchor, so the sentence derived from it is ungrounded too.
Fold into P0-1 — same function, one pass.

### R4. Audit on a successful build — **rejection-only is correct**

An audit that exists only on failure is cheap, and failure is when you need it.
The green case is fully described by `*.verify.json` (P0-3). **No audit.json on
success is the intended design**; document it so the next reviewer stops filing
it. This retires T8.

### R5. Dead top-level `__init__.py` — **defer; the "dead" claim is unverified**

The carried list says "147 stmts, dead". Measured: it is **280 lines exposing 20
public names**. Nothing in `src/` or `tests/` imports those 20 (tests use
`from doc_to_video_tutor import studio`), but I have not proven no external
consumer does. Deleting is cheap to get wrong and annoying to unpick. Stays on
the list; the check is one grep against those 20 names, and it belongs in a
cleanup pass, not a drive-by.

### TODO — ordered, with the reason each item is where it is

- [x] **P0-1 drop floor + re-hydration** (`plan.py:640`) — **DONE, see §S.** R3 (drop the
      narration sentences) was tested, found harmful, and retracted; the fix
      re-hydrates from `source_chunk` instead. P1-7 folded in and also done.
- [ ] **P0-3 `*.verify.json`** — closes L4, retires T8 per R4, finishes T0.
- [ ] **P1-1 video takeaways: two columns + `(cont. N)`** — port my `pptx.py` fix
      into `slides.py` *now* as a stopgap, even though R1 deletes that code
      eventually. Cheapest way to stop shipping the defect meanwhile.
- [ ] **P1-2 VTT 7s offset** — decide sidecar-vs-muxed, then fix.
- [ ] **P1-3 title gate** — stop the ~41-char mid-phrase truncation; it is spoken.
- [ ] **P1-4 verify-repair content deletion** — re-verify `tester` M1 on the real
      plan first; it was measured on a `/tmp` copy.
- [ ] **P1-5 mono loudness** — the delivered MP4 fails C3.2 downmixed.
- [ ] **P1-6 `1/(n+1)` spoken two ways** + `±`/`%`/`·` invisible to the audit.
- [ ] **P1-8 repair reads `section_digest`** · **P1-9 duration redistribution**.
      (P1-7 re-hydration: done in §S.)
- [x] **R1 stage 1** — DONE, see §AC. Deck emits the video's reveal variants with
      no reflow. Stage 2 (render frames from PPTX geometry) NOT started.
- [x] **R5 — premise was FALSE, do NOT delete.** `__init__.py` is a **live primary entry point**
      (`doc-to-video-tutor`) with **zero quality gates**, and the README points users at it.
      See §AB. Now needs a canonical-path decision, not a deletion.
- [ ] LLD updates owed: source coverage, `_flatten_parentheses`, containment
      slack, column balance, video-path guard, measured loudness, R2, R4.
- [ ] Not started, no owner: TTS backend seam · `visual_pattern` · under-fill ·
      continuation markers on scene bodies · Pillow/PPTX parity.

## S. P0-1 SHIPPED — v012_014's build failure is fixed (uncommitted)

**215 tests pass.** The pre-audio gate now passes on the plan that killed a
38-second build.

### S1. Root cause was ordering, not the missing floor

`_drop_ungrounded_slide_text` runs **last** in the repair chain (line 1941), so
`_repair_thin_narrations` (1936) — which reads `source_chunk` and *raises* its
floor when one is present — had already run and never saw the scene the drop
then starved. The re-hydration machinery existed the whole time. It was simply
looking at a plan state that no longer existed.

### S2. My own ruling R3 was wrong, and testing it caught that

R3 said to drop narration sentences derived from dropped text. Measured on the
real scene, that would have left only the canned opener — **0 content words
against a floor of 8**. It makes the failure worse, not better. R3 is retracted;
the correct companion action is re-hydration, not deletion.

### S3. Why re-hydration, and not a floor on the ungrounded bullets

All three options were measured:

| Option | Result |
|---|---|
| Keep ungrounded bullets to hold the count | Re-breaks the hard grounding gate this function exists to satisfy; costs an LLM call and may fail anyway |
| Drop narration sentences derived from them (R3) | 0 content words vs a floor of 8 — **worse** |
| **Re-hydrate from `source_chunk`** | **Anchored by construction**, which is exactly what the dropped bullet was not |

`source_chunk` is present on 9/9 scenes in both builds, so the material is
always available — the model just did not use it.

### S4. Measured, on the real v012_014 plan

Pre-drop state reconstructed from the recorded `dropped_slide_text` (the shipped
artifact is post-drop, so replaying the drop alone is a no-op and would have
tested nothing):

```
PRE-drop  bullets/scene : [4,2,3,2,2,2,2,3,3]   scenes 2,4,5,6,7 on exactly 2
dropped 3 -> 3 scenes starved
rehydrated 3 from source_chunk
POST      bullets/scene : [4,2,3,2,2,2,2,3,3]   no scene below the floor
tts_required_concept_missing : NONE - pre-audio gate PASSES
```

Scene 7's spoken track went from
`"... Exit three structural error. Exit four data inconsistency."` (6 content
words) to carrying real source prose:
`"... Exit three structural error. Plain words exit code zero PASS, one FAIL..."`

### S5. Two details that would have shipped silently

- **The pass is unconditional, not gated on `ungrounded_dropped`.** A plan loaded
  from disk is already post-drop, so the drop is a no-op on replay; a gated pass
  never runs and the starvation stays persisted in the artifact where only
  `verify` can still see it.
- **Markdown leaked into speech.** The first re-hydrated bullet was
  `"- Plain words: exit code 0 PASS..."` and the rebuilt narration opened on a
  dash. List markers are now stripped, pinned in the regression.

### S6. Pinned in `test_starved_scene_is_rehydrated_from_source_before_the_pre_audio_gate`

Uses the real build as the fixture. Asserts the 3 recorded drops, 3 re-hydrated
scenes, no scene below 2, the gate passing, **no inflation of already-healthy
scenes**, no markdown in bullets or speech, and idempotence (a second pass must
not duplicate a re-hydrated bullet).

The linter caught a bug in that test while writing it: `spoken.lstrip("Structural
Error Classes. ")` strips a *character set*, not a prefix, so the assertion
removed nearly the whole string and could not fail. Rewritten to test the
re-hydrated bullet directly.

### S7. Effect on the other rulings

- **P1-7 (re-hydrate from `source_chunk`) is DONE**, folded into P0-1.
- **§N3's `dropped_slide_text` half is improved, not closed.** The provenance
  record is still half-applied on v012_015 (bullet dropped, sentence still
  spoken) because that plan predates this fix.
- **§L/§M's J1 is closed.** T1/T2 are done; §M's T1 and T2 can be struck.

## T. P0-3 SHIPPED — `*.verify.json`, and one agent finding DISPROVEN

**217 tests pass** (26s, up from 7s — two of the new tests genuinely measure a
5.5-minute MP4; see the cost note below).

### T1. `*.verify.json` — the artifact the build was throwing away

Written on **all three** paths: green, pre-audio BLOCKED, pre-render BLOCKED. A
blocked build is when evidence matters most, so it is not gated on success.

Contents: `verdict`, `plan_sha256` (a green build finally has something for
`check_audit_binding` to bind), per-gate verdicts, counts (`scenes`, `clips`,
`audio_files`, `narration_words`, `bullets_per_scene`, `dropped_slide_text`,
`unclaimed_source_sections`), layout findings from **both** renderers, measured
media, and duration as a ratio.

Measured on the shipped v012_015 artifacts:

```json
"counts": {"scenes":9,"clips":10,"audio_files":10,"narration_words":505,
           "bullets_per_scene":[3,3,3,3,3,3,3,2,3],
           "dropped_slide_text":1,"source_sections":13,
           "unclaimed_source_sections":0},
"duration": {"mp4_seconds":329.379,"mp4_pct_of_target":137.2,"band":"LONG"}
```

`137.2% / LONG` is the number a viewer experiences, and the build log has never
reported it — it only ever printed the 120% audio figure.

### T2. L4 closed at the source

The soft findings were not "not printed" — they were **computed and then
filtered out two lines later**:

```python
problems = [p for p in guard_plan(...) if not any(p.startswith(soft) ...)]
```

Now split into `soft_problems` / `problems`, with the soft half printed in the
build log *and* recorded in the artifact. `LAYOUT_NOTES` from the video path is
folded into the same list, so the two renderers' layout findings finally meet in
one place.

### T3. A field caught lying during its own first run

`unclaimed_source_sections` was first filled with `len(source_sections)` — every
section in the document (13), not the unclaimed ones. Same class of error as
printing a config constant under a "loudness :" label. Fixed to call
`_unclaimed_source_sections(plan)`, and pinned against the function itself so the
field cannot drift from its meaning again.

### T4. **DISPROVEN — `tester` W7 "mono downmix fails C3.2 at -19.7 LUFS"**

Tried three ways on the shipped MP4:

| method | reading |
|---|---|
| `ebur128=peak=true` (stereo) | **-16.7 LUFS** |
| `ebur128=peak=true -ac 1` (mono downmix) | **-16.7 LUFS** |
| `loudnorm ... print_format=summary` | **-16.8 LUFS** |

All three land within C3.2 (±2 LUFS of -16). The claimed -3.46 dB video-vs-clip
trim is also not there: clip `scene_01.mp3` reads -15.9, the same window in the
MP4 reads -16.5, a 0.6 dB difference consistent with mp3 encoding plus a stereo
container. **W7 does not reproduce and is withdrawn as a defect.**

The right response is not to argue about it, which is why `measure_delivery` now
records both the stereo and the mono-downmix reading on every build. The check
is worth keeping precisely because it currently *passes* — a real regression
would now be caught, and the next disagreement gets settled by a number both
sides can re-derive.

### T5. Cost note, and a self-inflicted slowdown I fixed

The first version of the media test took the suite from **7s to 65s**: it was
decoding 7904 frames of 720p in order to measure audio. `-vn` cut each pass from
11.2s to 4.6s and the suite to 26s. Worth recording because the failure mode is
predictable — a check expensive enough that people skip it is worse than no
check, and that is exactly how the 42-minute review gate (§O) got skipped.

### T6. Pinned

- `test_verify_artifact_records_measurements_not_configuration` — digest present,
  soft findings survive into the artifact, `unclaimed_source_sections` equals
  `len(_unclaimed_source_sections(plan))`, duration 137.2% / LONG, measured
  loudness present, mono downmix compliant.
- `test_ebur128_reading_is_a_measurement_of_the_file` — **an absent file returns
  an empty dict and never a fabricated reading**, the stereo reading parses and
  sits in C3.2, and `measure_delivery` returns dimensions and both loudness
  forms.

## U. P1-1 SHIPPED — two-column takeaways in the video path (uncommitted)

**218 tests pass.** The empty-right-half defect is gone from the MP4, and the
second page now labels itself.

### U1. The root cause was two implementations, not a missed file

`_shorter_column` — the fix for "every takeaway sits in column 0 and the second
column is never used" — was written into `pptx.py` and never reached
`slides.py`. So the deck was fixed and the video still shipped the defect. That
is §N3 in miniature, and it is the third instance of the same shape
(`_shorter_column`, `(cont. N)`, the diagram fit).

`pptx.py` already imports five helpers *from* `slides.py`, so the shared helper
now lives in `slides.py` and `pptx.py` imports it. One implementation, and
`pptx._shorter_column is slides._shorter_column` is asserted in the regression
so a second copy cannot reappear.

### U2. What changed in the video path

- **Two balanced columns.** `_TAKEAWAY_COL_X = (44, 655)`, width 575, row 40px.
  The vertical `room(44)` guard still gates the block; `tops` only decides which
  column an item lands in, so the overflow guard keeps its meaning and reveal
  order still reads down-then-across. `yy` advances to `max(tops)` afterwards so
  the next block cannot be laid out on top of the shorter column.
- **Six takeaways now fit one page** instead of two four-per-page slides:
  `_takeaway_pages(6) == 1`, `_takeaway_pages(25) == 2`.
- **Continuation marker.** Page 2 onward is titled `Key Takeaways (cont. 2)`,
  matching the deck. A title that already carries `(cont. N)` is not
  double-labelled.

### U3. Verified in pixels, not just in the code

Rendered the real v012_015 plan and measured the fully-revealed takeaway frame
(`slide_10_06.png`) against its modal background:

```
content rows     : y 91..691 of 720
ink left column  :  9505 px
ink right column :  6044 px
right/left ratio : 0.64   -> BOTH COLUMNS USED
```

Not symmetric, and correctly so: item 1 is a full line in column 0 while the
last column-0 item wraps, so the right column starts higher. The point is that
neither column is empty.

### U4. One thing I checked rather than assumed

`TAKEAWAY_ROWS_PER_COL = 12` is a pagination estimate; the real guard is
`room(44)` in the draw loop. If the estimate were ever too generous, a page
could claim more items than it draws and the surplus would vanish with no
continuation page. It cannot happen today because `render_scenes` caps
`takeaways` at `[:6]` (3 rows per column), but the coupling is implicit and
recorded here so raising the cap is not done blind.

### U5. Still a stopgap

Per §R1 this drawing code is scheduled for deletion once `pptx.py` grows reveal
support. The consolidation is unchanged; what changed is that the video stops
shipping a known defect in the meantime, and there is now one `_shorter_column`
for the consolidation to move rather than two.

## V. P1-3 SHIPPED — the title gate, and all three agents were wrong about why

**219 tests pass.** Zero fragment titles remain; layout audit still clean.

### V1. The diagnosis in the agent report was wrong

`graphic-reviewer` concluded the cut was "a fixed ~41-char budget, not a width
limit" and that the box had ~0.6in spare so no clipping was needed. Measured on
the shipped plan: **every title is 36-41 characters against `clip_title`'s 44
limit and `_TITLE_CAP` of 44.** Neither ever fired. The 7B emitted the fragments
itself:

```
s2  title '2: The metric registry — metadata that'      (38 ch, under the cap)
    topic '2: The metric registry — metadata that makes verdicts mechanical'
s3  title '3: Kind = verdict semantics (gate vs'         (36 ch, unclosed bracket)
    topic 'Kind = verdict semantics'
s8  title '8: active.json — the canonical pointer to'    (41 ch, dangling on "to")
    topic 'active.json'
```

The intact concept name was in `plan.scenes[*].topic` the whole time. This is
why it is a **semantic gate**, exactly as the todo item always said ("no
incomplete-phrase titles") — and why raising `_TITLE_CAP` would only have
produced a 60-character fragment next.

### V2. Detection is structural, because vocabulary would not work

`_title_is_fragment` flags a tail from `_TITLE_DANGLING` (a, the, to, of, vs,
that, …) or unbalanced brackets. That list cannot be complete, and the proof is
scene 2: `clip_title("The metric registry — metadata that makes verdicts
mechanical")` returns **"... metadata that makes"** — which ends on a *verb*.
Detecting that needs English morphology, which does not belong in a
deterministic repair.

So the repair splits on the **document's own convention**: these headings are
`N. Concept — qualifier`, so the concept name is the head and the qualifier is
decoration. No vocabulary required, and it uses structure the source already
guarantees.

### V3. Three attempts on my own repair, each caught before it shipped

1. **First version** used `clip_title(heading)` directly. Scene 8 came out as
   `` 8: `active.json` — the canonical pointer to "the" baseline `` — I had
   introduced a **markdown leak** (backticks, quotes) into a display string, and
   a dangling `"the"`.
2. **Second version** stripped markdown and dropped the trailing clause, but
   scene 2 still ended on "makes", because the fragment test cannot see a verb.
3. **Third version** scored candidates, and I got the scoring backwards — it
   returned the first *fitting* form before considering that the head was
   shorter and cleaner, which regressed scene 2 to the 44-char fragment.

What finally works: pool the forms from **heading and topic together** and take
the shortest complete one. Sequential fallbacks could not reach the topic for
scene 3, whose heading is complete at 55 characters with no clause separator to
truncate — "Kind = verdict semantics" is shorter *and* complete.

### V4. Measured result

| scene | before | after |
|---|---|---|
| 2 | `2: The metric registry — metadata that` | `2: The metric registry` |
| 3 | `3: Kind = verdict semantics (gate vs` | `3: Kind = verdict semantics` |
| 8 | `8: active.json — the canonical pointer to` | `8: active.json` |

All three now fit the 44 cap, none is a fragment, deck numbering is preserved
(it is visual only — `build_tts_script` strips `N.` before speaking), and no
markdown survives. `_audit_layout` on the rebuilt deck: **clean**.

### V5. Pinned in `test_fragment_titles_are_repaired_from_the_concept_the_plan_already_carries`

Asserts the fixture still has fragments at exactly `[2, 3, 8]` (so it cannot
drift away from the incident), that exactly 3 are repaired, that none survives,
that **no repaired title exceeds `_TITLE_CAP`** (otherwise the repair trades a
spoken fragment for a layout overflow), that numbering is kept, that no markdown
reaches a title, that the pass is idempotent, and that the spoken track carries
whole phrases.

## W. P1-2 SHIPPED — captions were 7 seconds early

**220 tests pass.** `mod03_gates_v012_015.vtt` regenerated and now sits on the
video timeline.

### W1. Root cause: an ordering fact the writer could not see

The MP4 prepends a silent title card — `TITLE_HOLD` = 4.0s of **real PCM
silence** written as a proper WAV — inserted as pseudo-clip 0
(`audios = [title_wav, *audios]`), after which `assemble_video` adds its usual
`pause` after every clip including that one. So clip 1 begins at **7.0s**.

`_write_webvtt` set `base_s = 0.0` and never knew. It is called at
`video.py:786`, *before* the title card is prepended at 795, so the offset
cannot be inferred inside it. Now takes `lead_in`, and the call site passes
`TITLE_HOLD + pause` when the video is rendered and `0.0` under `--skip-video`,
where there is no title card and clip 1 genuinely starts at zero.

### W2. Measured, not asserted

| | first cue | last cue ends |
|---|---|---|
| before (`lead_in=0`) | `00:00:00.100` | `00:05:15.001` (315.0s) |
| after (`lead_in=7.0`) | `00:00:07.100` | `00:05:22.001` (322.0s) |
| measured audio | clip 1 onset **7.28s** (`silencedetect`) | ends ~322.3s |

The 0.28s residual is frame quantisation at 24fps. Pinned to within 0.5s of the
measured onset, so the check fails if the structural 7.0s ever drifts.

### W3. The contract question this settled

`pipeline-architect` noted no subtitle stream is muxed, so the defect only bites
if the `.vtt` sidecar ships beside the video. **Ruling: it does.** The VTT is the
interoperable caption form and a lesson that ships without usable captions is
not a lesson; the JSON sidecar stays for the renderer, which receives
`timings=[None, *timings]` and handles the offset itself. So the VTT must be
correct on the video's timeline, which is what `lead_in` now guarantees.

### W4. Disclosure

Regenerating the VTT for this test **overwrote** `output/mod03_gates_v012_015.vtt`
with the corrected track. That is the intended fix, but it means the stored
artifact no longer matches what the original build wrote — the same class of note
as §Q6. The MP4, plan and audio were not touched.

## X. P1-4 — M1 does not hold as reported; the detector had a blind spot

**221 tests pass.** One agent finding withdrawn, one real pre-existing defect
newly visible.

### X1. M1 re-measured on the real plan, cumulatively — the claim does not hold

`tester` M1: "`verify`'s repair chain deletes teaching content — 373 → 323
narration words (−13.4%) from scenes 3/7/9 — while slides still render that
'Why THIS' text. Any re-render therefore creates 3 new `unspoken visual claim`
findings." Flagged in §N4 as **unverified**, because it was measured on a
`/tmp` copy.

Re-measured on `output/mod03_gates_v012_015.plan.json` itself, each step applied
cumulatively:

| step | design-decision findings | narration words |
|---|---|---|
| shipped plan | 1 | 373 |
| `_dedupe_narration_templates` | 1 | 321 |
| `_deepen_narrations` | 0 | 393 |
| `_enforce_unique_narration_trigrams` | 1 | 362 |
| `_repair_unsafe_narrations` | **6** | 297 |
| `_repair_thin_narrations` | 1 | **373** |
| `_trim_narration_word_count` | 1 | 373 |

`_repair_unsafe_narrations` **does** spike to 6 findings and −65 words in a single
step — the reported mechanism is real. But `_repair_thin_narrations` then rebuilds
from the scene's own fields and the chain **ends exactly where it started**. The
−15.8% / 5-scene state is *mid-chain*, not the end state. **"Any re-render
creates new findings" is withdrawn.** (My own first pass also produced −15.8%,
because I read the mid-chain number as final — same mistake, same source.)

### X2. The real problem was a blind spot, and closing it found a live defect

`_unspoken_visual_claims` checked `status_badges` and `json_snippet` only. The
`design_decision` card is the largest thing a slide can claim without the
narration saying it, and it was **structurally invisible** — which is why nobody
found the one that is actually there.

The card is prose, so the badge test (token intersection) is far too lenient: one
shared word would read as "spoken". It now uses a coverage fraction
(`_DESIGN_DECISION_SPOKEN_FRACTION = 0.6`) — paraphrased-but-taught still passes,
a deleted sentence does not.

That immediately surfaced a **pre-existing** divergence no agent reported:

> **scene 2** — the slide says *"info: recorded for provenance, never a verdict
> — can't break a build by existing"*; the narration says only *"info = recorded
> only"*. **23%** of the card's content words are spoken. `provenance`,
> `verdict`, `break`, `build` are all on the slide and none in the audio.

Not fixed here. Fixing it means either re-narrating or trimming the card, and
that is the same trade as §J8b. What changed is that it is now **visible** — it
will appear in `verify.json` under `gates.review_before_build.soft` on every
build, which is the point of §T.

### X3. Second agent finding that did not survive a complete check

W7 (mono loudness, §T4) and M1 (this section) both looked solid, cited exact
numbers, and both dissolved when the measurement was repeated end-to-end rather
than at the convenient point. Neither was a reviewer failing at its job: both
measured real things accurately. The failure was **where the measurement was
taken** — a mid-chain snapshot read as a final state, and a control file read as
the shipped file. Recorded because the pattern will recur: a number taken from
the middle of a process needs its endpoint stated before it becomes a finding.

## Y. P1-6 SHIPPED — one expression, two pronunciations

**222 tests pass.** `tester` W3 confirmed; it was three separate defects.

### Y1. The `1/(n+1)` inconsistency was an ordering bug

Same class as §S. `_spoken_variant` ran `_flatten_parentheses` **before**
`speech_expand`, and flattening replaces every bracket with a space:

```
_flatten_parentheses("1/(n+1)")  ->  "1/ n+1"
```

so the pronunciation rule `written='1/(n+1)'` could never match. Only the scene
that happened to spell the expression out in words was correct — scene 4 said
"one / n plus one", scene 5 said "one over n plus one", from identical input.
The rule table is matched against the *written* text, so anything that destroys
the literal has to run afterwards. Swapped; flattening still runs, still last.

### Y2. Symbols no voice reads correctly

Scene 2 carried a literal `±20%.` into the spoken track, plus two `·`
separators. `_speak_math_symbols` now handles them, after the rule table so a
more specific `PronunciationRule` still wins (`±0.03` → "plus or minus zero.03"
keeps the MHE number rule).

```
'x ± 20%.'  -> 'x plus or minus 20 percent.'
'a · b'     -> 'a, b'
'n<=5'      -> 'n at most 5'
'3 != 4'    -> '3 not equal to 4'
'at >= 2'   -> 'at at least 2'
```

The replacement is padded so tokens cannot fuse, then the padding is pulled back
off adjacent punctuation — otherwise "20%" becomes "20 percent .".

### Y3. The audit could not see this class, which is why it survived

The residue regex matched only `[{}[]_=]` or `\d+/\d+`. `±`, `·`, `≤`, `≥`, `≠`
and `\d+/\(` were all invisible — and `1/(n+1)` is digit-slash-paren, not
digit-slash-digit, so the one expression under discussion never matched either.
All now matched. A detector that cannot see the defect is the same failure as no
detector, and it is the second time today that has been the actual root cause
(§X2 was the other).

### Y4. Two test bugs of my own, both caught before shipping

- **A pinned test I had to correct, not weaken.**
  `test_speech_expand_longest_first_and_boundary` asserted
  `"module one Data  and  testset"` — with the double spaces that padded
  replacements leave behind. Its actual subject is longest-match-first ordering
  and the word boundary (`rasm1` must not split), not the spacing. Normalised the
  comparison on both sides and left every other assertion alone.
- **My new test's extraction was wrong.** It sliced
  `.{0,4}n plus one.{0,4}` and so compared *trailing context*, reporting two
  pronunciation forms for two correctly-spoken scenes. Rewritten to assert the
  phrase itself. Worth recording: a test that fails for the wrong reason is
  worse than no test, because the next person "fixes" the code.

### Y5. Consequence, stated honestly

Scene 2's spoken word count rose 77 → 81 once `±20%` and two `·` became actual
words, pushing it further past the 70-word WARN band. That is correct: the
symbols are now genuinely spoken rather than silently dropped. It is a real
signal that scene 2 carries too much, and it belongs to P1-9 (duration
redistribution), not here.

## Z. P1-8 SHIPPED — Defect 2d closed: repair reads the record, not the raw string

**223 tests pass.** Closes the last `plan.py` lineage item; §K's `[~]` can
become `[x]`.

### Z1. What was actually missing

§K recorded "provenance digest recorded per assignment (done); make repair read
the record instead of the raw string". Both halves of that were accurate, and the
second half was worse than "still uses a raw string":

- `section_digest` is a digest of `f"{heading}\n{body}"`, so it **cannot be
  recomputed from the chunk**. Nothing verified the chunk came from the section
  the record names.
- **Both** hydration paths read `sc["source_chunk"]` directly with no check at
  all: `_repair_thin_narrations` → `_rebuild_scene_narration` (two sites), and
  the `_rehydrate_starved_scenes` top-up added in §S.

`source_chunk` is a mutable plan field. A plan whose assignment moved on while the
chunk did not would hydrate a scene from the wrong paragraph — correct-looking
prose, wrong content, no finding, no log line.

### Z2. Fix

Assignment records now also carry `chunk_digest`, written at the same moment as
the chunk, and `verified_source_chunk(plan, scene_no)` gates both paths. It lives
in `util.py`, not `plan.py`, because `plan` imports `narration` — so provenance
code the narration repair also needs would otherwise be unreachable. `_text_digest`
moved with it for the same reason; `plan.py` re-exports both.

### Z3. An existing test caught me being too strict

First version returned `""` when a scene had **no** assignment record. That broke
`test_repair_thin_narrations_hydrates_scene_with_source`, and the test was right:
**no record is not the same as a contradicted one.** There is nothing for the
chunk to disagree with, and refusing it breaks hand-built and pre-assignment
plans for no safety gain. The defect being guarded is a *mismatch*, and that is
still refused. Being stricter than the defect requires is its own bug — the same
lesson as §R3, in a different place.

### Z4. Verified, all four directions

| case | result |
|---|---|
| genuine chunk, digest recorded | returned |
| chunk tampered, digest stale | **`""` — refused** |
| chunk emptied | `""` |
| neighbouring scene intact | unaffected |
| pre-`chunk_digest` plan | passed through, not rejected |
| scene index 0 / 999 | `""`, no crash |

Pinned in `test_repair_paths_refuse_a_source_chunk_that_contradicts_the_record`,
including that the starved-scene top-up refuses a chunk that failed its check, so
a refused chunk cannot resurrect a scene from unverified prose.

My own first version of that test computed the digest *from the tampered value*,
so the tamper matched trivially — the same class of error as §Y4, and the second
time today a verification test was wrong in the direction that made the code look
correct.

## AA. P1-9 — duration decomposed, not "fixed"

**223 tests pass.** No content was cut; the number stopped being ambiguous.

### AA1. "137% of target" was two problems wearing one number

Measured on v012_015:

| | seconds | % of the 240s target |
|---|---|---|
| MP4 (what a viewer watches) | 329.4 | **137.2%** |
| narration audio only | 292.4 | 121.8% |
| **structural silence** | **37.0** | **15.4%** |

The 37s is `TITLE_HOLD` 4.0 + a 3.0s pause after each of 9 scenes (27.0) +
`end_hold` 6.0. **None of it is teaching.** One number for both causes is why
§L2 and §N each had to be corrected — I reported 120% (audio) when the log's
329.4s was the container, and the container is 137%.

So the fix sent to the content layer was the wrong one. Half the overrun is a
`--pause` default.

### AA2. What is an outlier, measured

| scene | words | share of audio |
|---|---|---|
| mean | 50 | 10.0% |
| **2** | 77 | **15.2%** |
| 6 | 37 | ~7% |

Scene 2 is the only clip over the 70-word WARN band, and at 1.5× the mean it is
the single redistributable scene. It is inside the 90-word FAIL ceiling, so
`_trim_narration_word_count` correctly leaves it alone — trimming it would be a
content change no gate asks for. `verify.json` now carries `scene_share_pct`,
`scene_share_mean_pct` and `scene_share_outliers`, so this is a number every
build rather than a suspicion.

### AA3. The biggest single lever is not content

`--pause` defaults to **3.0s**, applied after all 9 scenes: **27s, or 11% of a
4-minute target, in silence alone.** Whether a lesson wants 3s between every
scene is a product decision, not a defect, so I have not changed the default.
Recorded here so it is a decision rather than an oversight: dropping it to 1.5s
would recover ~13.5s and take the MP4 from 137% to ~132% without touching a
single word of teaching content.

### AA4. Ruling

**Report, do not silently re-cut.** Every redistribution option here trades
teaching content for duration, and the pipeline's standing rule is that
deterministic repair may not remove teaching material to satisfy a layout or
length preference. Scene 2 at 77 words is inside every existing band. The
correct next step is either a lower `--pause` default (product) or a real
redistribution pass that moves content between scenes rather than deleting it
(engineering, and a larger piece of work than this slot).

## AB. R5 resolved — the top-level `__init__.py` is NOT dead, and I nearly deleted a live CLI

**My §R5 premise was wrong on both counts.** Correcting it is the whole point of
this section.

### AB1. What the list claimed, and what is true

| | claimed in §K | measured |
|---|---|---|
| size | "147 stmts" | 280 lines, and not a shim — a full implementation |
| status | "dead" | **live, primary entry point** |

`pyproject.toml` ships **two** console scripts:

```
doc-to-video-tutor = "doc_to_video_tutor:main"        <- this file
doc-to-studio      = "doc_to_video_tutor.studio:main" <- the studio package
```

Both run. They are different programs:

```
doc-to-video-tutor [-a|-v] [--lang {hi,mr,en}] ...   # legacy flags
doc-to-studio {build,review,tts-check,verify,render}  # studio subcommands
```

`main` and `ask_llm` are implemented twice. `generate_lesson`, `create_video`
and `text_to_audio` exist **only** at top level — so they are not dead, they are
the legacy path's own.

### AB2. Why this matters more than a cleanup item

**The legacy path contains no quality machinery at all** — zero occurrences of
grounding, audit, gate, repeat or token checks. Every gate, every fix and all 224
tests from this session live in `studio`, reachable only through
`doc-to-studio`.

And `README.md` still tells users the other way:

```
uv run doc-to-video-tutor doc/design/03_lld_tests.md
uv run doc-to-video-tutor doc/design/03_lld_tests.md -v
```

So the **documented, primary command name runs the un-gated pipeline.** Anyone
following the README gets none of §S, §T, §V, §W, §X, §Y, §Z or §AA. A user who
invoked `doc-to-video-tutor` this week would have hit the scene-7 starvation and
the 7-second caption offset without a word of complaint, because nothing in that
path is looking.

### AB3. Ruling: do NOT delete. This is a product decision, and the safe default is the opposite of what the list said

Deleting it would break `doc-to-video-tutor` — a shipped command with a README
and a documented usage line. The decision is **which implementation is
canonical**, and that is not mine:

1. **Retire the legacy path**, repoint `doc-to-video-tutor` at
   `studio:main`, and keep `doc-to-studio` as an alias. One pipeline, one name,
   and the README becomes correct. This is my recommendation.
2. **Keep both**, and say plainly in the README that `doc-to-video-tutor` is a
   preview with no quality gates.
3. **Keep both and gate the legacy path too** — a lot of work for a path that
   should probably not exist.

Option 1 is a small, mechanical change with a large correctness payoff, and it is
the only option under which the README's instructions produce a lesson that has
been through the gates. **Until it is chosen, do not delete the file** — the
previous instruction to "decide whether to delete the dead top-level
`__init__.py`" was based on a false premise and should be read as void.

### AB4. Also noted

`load_dotenv()` runs at import in **both** `__init__.py` (line 20) and
`studio/config.py` (line 10), so importing the package loads `.env` twice. Harmless
today; noted because it is a side effect at import time in two places, which is
the shape that becomes expensive when someone adds caching or a test that
monkeypatches the environment.

## AC. R1 stage 1 SHIPPED — the deck can now emit the video's reveal variants

**224 tests pass.** This was the gate on the renderer consolidation (LLD 23.7):
`pptx.py` had no reveal support, so "render video frames from the deck's layout"
could not start.

### AC1. The spec, ported from the video

`slides._slide_variants` emits `count + 1` frames per scene: frame `k` shows
`bullets[:k]` with bullet `k-1` highlighted, and a scene with nothing to reveal
emits a single uncut variant. `reveal_variants` and `reveal_plan` produce the
identical sequence, and `build_pptx(..., reveal={page: (cut, highlight)})` draws
it.

### AC2. The load-bearing property: reserved, not reflowed

Space is reserved through `_RowStack` for **every** bullet whether or not it is
drawn, so the layout is identical across variants. Measured on slide 3 across
four steps:

```
step 0  reveal=(0, None)  bullets drawn 0  lowest text y 7.01
step 1  reveal=(1, 0)     bullets drawn 1  lowest text y 7.01
step 2  reveal=(2, 1)     bullets drawn 2  lowest text y 7.01
step 3  reveal=(3, 2)     bullets drawn 3  lowest text y 7.01
```

A reveal that re-flowed would break audio-visual alignment, and it is the one
thing a test counting shapes would miss. The highlighted bullet tracks the last
one drawn (`-`, `-B`, `--B`, `---B`).

### AC3. `_BODY_BUDGET` hoisted, because two copies of a page enumeration is the defect

`reveal_plan` must enumerate scene pages exactly as `build_pptx` does. The
budget constant was function-local, so the first version either failed to import
it or would have needed a second hard-coded copy. Hoisted to module level.

This is the same lesson as §23.2 and §U, arriving from the opposite direction:
the two renderers drifted because logic was duplicated, and the fix is to make
duplication impossible rather than to keep the copies in sync.

### AC4. A test bug of mine, caught

The highlight assertion expected `"B"` at step 0, where **zero** bullets are
drawn and so there is no highlight at all. The code was right and the test was
wrong — the third time today a verification test failed for the wrong reason
(§X4, §Y4). Recorded because the pattern is the dangerous one: a test that fails
correctly invites someone to "fix" correct code.

### AC5. What this unblocks, and what it does not

Stage 1 done means the deck can drive a frame renderer. Stage 2 — rendering
video frames from PPTX geometry — is **not** started, and stage 3 (deleting the
drawing code from `slides.py`) depends on it. Until then both renderers still
exist, and both layout guards must still be read as covering different products
(LLD 23.7). The two-column takeaway fix and the continuation marker in
`slides.py` remain necessary rather than redundant.

## AD. Defect 9 completed — continuation markers, and the marker is never spoken

**226 tests pass.** §V fixed the title half ("no incomplete-phrase titles").
This is the marker half, plus a markdown leak found while checking it.

### AD1. What was missing

A scene whose bullets paginate produced N pages that all carried the **identical
title**, so a viewer had no way to tell a continuation from a repeat — and each
page is a separate audio segment, so the confusion is audible as well as visual.
`Key Takeaways (cont. N)` had existed since the takeaway pagination landed;
scene bodies never had it. The todo list recorded this asymmetry as real, and it
was.

`mark_continuations(pages)` now numbers pages 2..n per scene, applied to the
**final** page list rather than inside `_scene_pages` — because the deck also
splits by measured height and can end up with more pages, so a marker applied
earlier would number the wrong ones. Pages are grouped by `_page_group`, stamped
by `_scene_pages` and preserved through `_paginate_by_height`'s clone, so
numbering restarts per scene instead of running across the deck.

Both renderers verified in agreement:

```
VIDEO  Baseline snapshot and compare
        Baseline snapshot and compare (cont. 2)
        Baseline snapshot and compare (cont. 3)
DECK   identical, after the extra height split
```

### AD2. The marker would have been spoken

`build_tts_script` strips a leading `N.` from the spoken title but not
`(cont. N)`, so a paginated scene would have had the narrator say *"cont two"* out
loud. Caught by checking the **spoken** track, not the slide — the slide looked
correct. Now stripped, and pinned.

### AD3. Two markdown leaks on the shipped deck, found while looking

`_sanitize_design_decisions` only dropped *empty* cards, so two scenes rendered
literal backticks:

```
s8  `active.json` is a canonical pointer NOT a regular file because ...
s9  Deterministic offline gate is NOT a live model because it runs `run_suite` ...
```

The **audio was already clean** — `_flatten_parentheses` strips backticks — which
is exactly why this survived: nothing inspected the slide text for formatting,
only the spoken text. Same rule as the §V title fix, now applied to the card.

Underscores inside identifiers are deliberately **kept**: removing them would
rename a real file on screen (`run_suite` → `run suite`), which is a worse defect
than the backtick.

### AD4. Pinned

- `test_paginated_scene_bodies_are_labelled_and_the_marker_is_never_spoken` —
  both renderers agree on numbering, numbering restarts per scene, takeaways are
  not double-labelled, and `cont` never appears in `spoken_title`.
- `test_design_decision_card_does_not_render_markdown` — asserts exactly 2 cards
  stripped on v012_015 (so the fixture cannot drift), no `[` or backtick survives
  in any card, and `active.json` / `run_suite` are still intact.

Defect 9 is now complete: mandatory continuation marker **and** no
incomplete-phrase titles.

## AE. The code-review task had no owner — `code-reviewer` added

**226 tests pass.** Answering a gap, not adding a nicety.

### AE1. Verified: zero coverage, across all five agents

| agent | mentions redundancy / dead code / complexity / refactor? |
|---|---|
| `mentor` | **no** |
| `reviewer` | **no** — and it explicitly *excludes* code: "Whether the **CODE WORKS**. `tester` runs the offline L1 gates…" |
| `tester` | **no** — runs gates and media forensics |
| `pipeline-architect` | **no** — cross-checks artifacts |
| `graphic-reviewer` | **no** — looks at pixels |

The four specialists are separated by **evidence type**, and none of them looks
at the source. `mentor` is the only `primary` that reads code, and its prompt is
about pipeline *behaviour* (LLM generation, audio, video) and the evidence
standard — not duplication, dead paths or complexity.

### AE2. Why a new role rather than widening `reviewer`

The existing five are cleanly partitioned by what evidence they may use, and that
separation is the reason they did not duplicate each other today. Folding code
review into the documentation auditor would blur exactly the boundary that keeps
it useful — and `reviewer`'s whole value is that it can say "this claim is about
the code, not about whether the code works" without ambiguity.

A code reviewer also needs to run things (call-graph walks, AST, complexity) and
must be invocable **on demand**, not on every build — §O3's tiering already
established that per-build review has to be cheap. So: `subagent`, `edit: deny`,
its own 30-call budget.

### AE3. Two rules written in specifically because this session produced counter-examples

- **"Dead means unreachable, not unfamiliar."** The prompt names both live
  examples a careless reviewer would have flagged: `__init__.py` looks like a dead
  shim but is the `doc-to-video-tutor` console entry point, and `main`/`ask_llm`
  exist twice deliberately during a migration. I nearly deleted the first one
  today (§AB).
- **"Duplication is only a finding if the copies are not deliberately
  divergent."** The known-good counter-example is `pptx.py` vs `slides.py`:
  extraction is correct, but a shared helper that grows a `for_deck=` flag is
  worse than two functions, and the renderer consolidation (§R1) is the real fix.

Also carried over: measure the overlap rather than assert it, show the minimal
change, quantify the payoff, and **return CLEAN when the code is fine** — a
review that manufactures findings costs more than it saves.

### AE4. And a mistake I made while writing it

I granted `rg *` in the new allowlist — the exact dead grant §J6 removed from
`reviewer` after establishing that `rg` is not installed here, so it silently
falls through to `"*": "ask"` on every search. Caught on the same line I checked
the config, and removed. Every remaining granted tool is verified present.

## AF. External review of the reviewer config — partly agreed, partly declined

A reviewer argued the 7-step evidence discipline should be in the **`reviewer`**
agent, plus two wiring files. One half is already true; the other half was a real
gap and is now closed. Placement declined, with reasons.

### AF1. The 7 steps were already placed — measured, not assumed

| agent | steps present |
|---|---|
| **`mentor`** | **7 / 7** — the full EVIDENCE STANDARD, added early this session |
| `code-reviewer` | 4 / 7 |
| `graphic-reviewer` | 4 / 7 |
| `reviewer` | 3 / 7 |
| `pipeline-architect` | 3 / 7 |
| `tester` | 2 / 7 |

The specialists carry the subset that their evidence type can honour. That is the
design, not an omission: "render it and look at it" is meaningless to a
documentation auditor whose allowlist has no render tool.

### AF2. Why NOT in `reviewer` — two independent reasons

1. **It would break `reviewer`'s own contract.** `reviewer` is
   `edit: deny` with a read-only allowlist (`grep`, `cat`, `ls`, `wc`, `find`,
   `sed`, `git`) and its scope line explicitly excludes whether the code works.
   Instructing it to render artifacts and read pixels would either fail on missing
   tools or pull it across the boundary that makes it useful.
2. **The prompt's own worked examples are not documentation findings.** Every one
   is an artifact or geometry finding: the empty right column (rendered slide),
   `min(8.92, 12.72) − max(0.42, 0.62)` (shape geometry), the 200dpi
   re-render, the missing `(cont. N)` marker. Those are `graphic-reviewer` and
   `pipeline-architect` territory.

**The 7 steps are a cross-cutting standard, not a role.** They belong to the agent
that owns standards (`mentor`) and are enforced per-specialist. Putting them in
one reviewer would have left the other five unchanged — which is how
"the reviewer agent has the discipline" becomes true of nobody.

### AF3. The wiring half was a genuine gap — now closed

Neither file existed: `AGENTS.md` **not present**, `.opencode/` **not present**.
So the "don't retype it every session" half of the suggestion addressed a real
hole.

- **`AGENTS.md`** (64 lines) — the 7 steps condensed, all 7 verified present, plus
  the routing table (all 6 agents verified against `opencode.json`, no orphans)
  and the repo traps that have each cost real time: two renderers, measurements
  printed as constants, ordering bugs, the ungated legacy entry point.
- **`.opencode/command/review.md`** (73 lines) — the full prompt with
  `$ARGUMENTS`, so `/review src/doc_to_video_tutor/studio/pptx.py` reproduces the
  process without retyping it.

`AGENTS.md` is the standing policy, so the standard now applies to **every**
session and every agent — including `code-reviewer`, which was added after the
standard was written and would otherwise be the one role without it.

## AG. The recurring agent error was a wrong interpreter in an allowlist grant

**226 tests pass.** Not a reasoning failure — a tooling trap, found by asking why
the same error kept repeating.

### AG1. What was actually happening

Two compounding faults, neither visible in any prompt or output:

1. **`uv` was granted to NO agent.** Every agent prompt and `AGENTS.md` documents
   the `uv run ...` form, so every call made that way fell through to
   `"*": "ask"` — a permission prompt, every time.
2. **`python3 -c` silently does the wrong thing.** It resolves to
   `/usr/bin/python3`, which **cannot import this package**:

   ```
   python3 -c "import doc_to_video_tutor"
     -> ModuleNotFoundError: No module named 'doc_to_video_tutor'
   .venv/bin/python -c "import doc_to_video_tutor"   -> ok
   ```

   And `code-reviewer` had `python3 *` granted **and** an ECONOMY line telling it
   to prefer inline `python3`. The call succeeds at the shell level and then
   fails with a misleading error — so the agent retries, and fails identically.
   That is the reported "same error, most of the times".

`tester` had been the workhorse precisely because its grants were already correct:
`.venv/bin/*` and **no** `python3`.

### AG2. Fix

- Dropped `python3 *` from every agent. On this box it is a trap, not a tool.
- Granted `uv run *`, `.venv/bin/python *` and `.venv/bin/python3 *` across the
  five subagents, so both the documented form and the reliable interpreter work.
- Corrected the four prompts that taught the broken form to name
  `.venv/bin/python` explicitly and to say why.
- Recorded the trap in `AGENTS.md`, so it applies to every session and every
  agent rather than living only in three prompts.

Audited afterwards: **every grant in all six agents resolves to something that
exists and executes.** The one exception is deliberate — see AG3.

### AG3. And the exception, stated rather than hidden

`soffice`, `libreoffice` and `pdftoppm` are granted to `graphic-reviewer` and are
**absent from this machine** (§J7/§T5). Unlike the `rg` grant — which I removed
twice for exactly this reason — these are worth keeping, because installing
LibreOffice is the documented unblock and the grant is what makes the agent able
to use it the moment it lands. The cost is bounded by the ECONOMY rule it already
carries: **probe once, record "render unavailable", never retry.**

If LibreOffice is not going to be installed, remove the three grants rather than
leaving a documented unblock that never arrives.

### AG4. A check of my own that was wrong

My first "every grant resolves" audit reported `.venv/bin/*` as unusable across
four agents. It was my check: for a glob-style grant it tested
`os.access('.venv/bin', X_OK)`, which is a directory-traversal test on the wrong
path. `.venv/bin/python` is reachable and works. Recorded because the failure mode
is the one that matters — a verification step that reports a false problem costs
more trust than having no verification step.

## AH. code-reviewer run on the diff — 2 findings, both mine

**228 tests pass.** Run inline against the uncommitted diff, following the
`code-reviewer` prompt. **Caveat recorded: inline means I wrote the code, so this
is weaker than an independent context** — the agent registry is fixed at session
start, so an agent added to `opencode.json` mid-session cannot be spawned until a
new one.

### AH1. FINDING 1 — `_takeaway_pages` defined in BOTH renderers, same name, two algorithms

| | signature | algorithm |
|---|---|---|
| `slides` | `(count, rows_per_col, cols)` | arithmetic upper bound |
| `pptx` | `(takes, budget, col_width)` | simulates the fill against a real text-height estimate |

The deck's count is **exact**; the video's cannot be, because it draws in pixels
and guards overflow at draw time with `room()`. So the two will legitimately
disagree — and under one name, a reader (or a future import) conflates them.

This is the fork shape that produced three separate defects today:
`_shorter_column` fixed in one renderer and missed in the other, the `(cont. N)`
marker, the diagram fit. **Renamed** to `_takeaway_pages_by_count` and
`_takeaway_pages_measured`, with each docstring saying which is which and why
they differ. No logic deduplicated — they are genuinely different questions
answered differently, and forcing them together is what R1 exists to do properly
later.

**Pinned:** an AST check that no private helper is defined in both renderers under
one name, with the message saying which remedy applies (import one, or rename).

### AH2. FINDING 2 — `LOUDNESS_MEASURED` never cleared between runs

`LAYOUT_NOTES` is cleared at the top of `render_scenes`. `LOUDNESS_MEASURED` was
appended to in `_normalize_loudness` and **cleared nowhere**, yet it is read back
twice: the build-log summary, and `verify.json`'s `clip_lufs_range`. Two builds
in one process would have reported **the union of both runs' clip loudness as
though it were one lesson's** — and `verify.json` is the artifact reviewers now
trust.

Cleared at the top of `synth_scenes`, deliberately **not** inside
`_normalize_loudness`: a direct call to measure one clip must not wipe a run in
progress. Both placements are asserted.

### AH3. Ruled out, with the check that cleared each

- **More forked helpers between the renderers.** AST scan of both modules for
  private `def`s: `_takeaway_pages` was the *only* collision. No shared constants.
- **`_shorter_column` still duplicated.** One definition in `slides.py`, imported
  by `pptx.py`, three call sites. Genuinely single now.
- **`reveal_plan` and `build_pptx` enumerating pages differently.** Both use
  `_scene_pages(scene)` + `_paginate_by_height(planned, _BODY_BUDGET)`, and
  `_BODY_BUDGET` is module-level. Identical.
- **Dead code.** AST scan of every private helper across `src/` and `tests/`:
  nothing defined without at least one other reference. Notably **no** orphan
  flagged in `doc_to_video_tutor/__init__.py` — the two false-dead calls from
  today did not recur.

### AH4. A mistake in the fix, caught by the suite

The rename broke two existing tests that imported `_takeaway_pages` by the old
name. I should have grepped importers before renaming — a rename is a breaking
change to every caller, and I had that command in my head from the `_shorter_column`
move earlier and still skipped it. Updated both imports; no assertion weakened.

## AI. External review of `code-reviewer` — 3 of 4 adopted, 1 corrected, coverage added

**228 tests pass.** The same verify-before-adopting rule applied to the review
itself: two of its four claims were checkable, and one was wrong.

### AI1. ADOPTED — permission was more permissive than the stated discipline

The prompt said *"Do not re-run the test suite to review code; read it."* while
the grant was `"uv run *": "allow"` — which matches `uv run pytest`. Instruction
and permission disagreed, and the permission was the one that would be enforced.

Narrowed to `"uv run python *"`. Verified: permits `uv run python -c` (which the
call-graph work needs), no longer permits `uv run pytest`. Same fix pattern the
reviewer identified in `reviewer` — make the allowlist match the stated behaviour
exactly, whether that means wider or narrower.

### AI2. ADOPTED — "dead" only checked the import graph

Both named examples were import-graph reachability, so the rule would have
retired a symbol reached only by name. Added: also grep for the symbol as a
**bare string** — registry keys, prompt templates, CLI subcommands,
`getattr(module, name)`. This codebase resolves some things by name, so zero
Python callers is not unreachable.

### AI3. ADOPTED — no explicit commit-pinning step

The output format already demanded `@ <commit>` but never said how to obtain it.
Added: run `git log -1 --format=%H` and record it before reviewing anything. A
finding against a stale checkout reads as authoritative, which is worse than
silence.

### AI4. CORRECTED — the two-step textbox defect is already FIXED

The review cited `_ppt_textbox(..., " ", ...)` plus a second `add_paragraph()` as
a live pattern, and said the estimator "measured the placeholder-only box".
Measured:

```
_ppt_textbox calls with a lone-space placeholder : 0
```

And `pptx.py:280-289` is a comment **documenting the fix** — "_ppt_bullet formats
paragraph 0 in place. The alternative - writing a placeholder and then calling
_ppt_para, which does add_paragraph() - left every bullet box rendering TWO
paragraphs…". The reviewer read the rationale for a past fix as a description of
current code.

The **class** is still worth naming, so it is in the prompt as an explicit
watch-for marked **"That defect is FIXED"**, with a note that any new call site
matching the shape is a repeat of a closed defect rather than a style nit. This is
step 7 of the review standard running in reverse: not a half-fix, but a
fully-fixed defect re-reported as live.

### AI5. ADDED — code coverage, on the user's instruction

A new section, because untested code is a real finding rather than a nit:

- a new or changed function with **no test that exercises it** is a finding —
  name the function and what could regress silently;
- a test that passes for the wrong reason is worse than no test
  (`assert f(x) is not None` is coverage theatre);
- for a large uncovered module, name the specific uncovered **branches**, not
  the percentage. "54% covered" is not actionable; "the `except` branch at L412
  is never taken" is.

Available to it because §AG narrowed the interpreter problem: it can now run
`uv run python -m pytest --cov=...` without a permission prompt, while still
being unable to run the suite for any other reason.

## AJ. External review of the two open decisions — both upheld, one revised

The review of §AB and §AA was checked against the code before being accepted.
Two of its claims held, one was refuted, and the requested root-cause trace
produced a definite answer.

### AJ1. §AB APPROVED — and the precondition found a real gap

**Accepted as a safety gap, not a design preference.** A documented, live CLI
that bypasses `guard_plan` and `_render_blocking_problems` contradicts the LLD's
own invariant that damaged narration can never reach TTS or video. Anyone
following the README can ship an ungated build.

**The precondition was worth running.** `--lang` exists on the legacy CLI and
**has no equivalent** on `studio`, which offers `--narr-voice`. Checked whether
anything else is lost:

| legacy | studio | verdict |
|---|---|---|
| `inputs path(s) to .md, .txt, .pptx` | identical | **preserved** — multi-file survives |
| `--voice` `--minutes` `--out` | all present | preserved |
| `-a` / `-v` | `--skip-video` / default | preserved |
| **`--lang {hi,mr,en}`** | **absent** | **the one real gap** |

`--lang` is mappable — `hi`/`mr` both mean the `mhe-mix` profile, `en` means
`english` — so a shim can translate it exactly. It cannot express anything
`--narr-voice` cannot, so nothing is lost by mapping rather than porting.

**Ruling, adopting the review's deprecation shape:** repoint
`doc-to-video-tutor` at `studio:main`, and leave the old name in place for one
release cycle raising an **explicit error that names the replacement** — not a
silent no-op, because a shell alias or CI script calling it today must fail
loudly rather than quietly change behaviour. `--lang` is translated to
`--narr-voice` in the message. Deletion is a later, separate decision.

### AJ2. §AA UPHELD — the pause cut is 5 points of a 37-point overrun

The review is right and my own framing was wrong. 137% → ~132% leaves the lesson
**32% over**, and a checklist entry that reads "duration handled" would hide that.
Accepted verbatim: reframe as *"reduce pause **and** open a ticket for the real
overrun"*, so the visible symptom going away cannot make 32%-over the new normal.

### AJ3. The root-cause trace, which the review asked for

`pipeline-architect` check #10 reports *whether* a build is over. What is missing
is *why*. Traced on v012_015:

```
505 words, 292.4s of narration, 240s target
overrun: 52.4s = 21.8 of the 37.2 points

measured speech rate       103.6 wpm
LOUDNESS_WPM projection    103.0     -> off by 0.6%
rate needed for 240s       126 wpm
```

**The projection is not the problem, and neither is the cross-fade.** The lesson
carries 505 words; at a natural MHE rate that is 292s. Hitting 240s needs ~415
words — **18% less narration** — or fewer scenes. That is a content decision, not
a calibration bug, which is why no amount of tuning `LOUDNESS_WPM` moves it.

### AJ4. The cross-fade claim — refuted, with the arithmetic

The review worried the 0.6s cross-fade might eat into the pause, leaving ~0.9s at
`--pause 1.5`. Measured in `video.py:703-728`: `d += pause` extends the **last
variant** (an image hold with no audio), the gap is a separate silent
`AudioClip(duration=pause)`, and `CrossFadeIn(0.6)` is applied at the **head of
every clip, over speech**. The fade never touches the pause, so the gap stays a
full 1.5s.

The review's *method* still stands even though its arithmetic was wrong: whether
1.5s feels rushed is perceptual, and no percentage can answer it. **Listen to two
or three scene boundaries at 1.5s before fixing the number.**

### AJ5. What this changes

- §AB moves from "needs a decision" to **approved with a specified shim**, and
  the precondition is discharged: one mappable flag, nothing else lost.
- §AA splits in two: the pause change is a free 5 points **and now carries an
  open ticket** for the 21.8 points of narration, which is a content decision
  about how much a 4-minute target should say.

## AK. §AB and §AA actioned — deprecation shim shipped, duration ticket opened

**228 tests pass.**

### AK1. `doc-to-video-tutor` is now a translating shim

Not repointed silently, and not deleted — a shell alias or CI script calls that
name today. The entry point now parses the legacy flags, prints a deprecation
banner to stderr naming the replacement and the exact translated call, and hands
off to `studio.main()`. Every documented invocation was run end to end against a
real document: the banner appears, `--lang hi` becomes `--narr-voice mhe-mix`,
`-a` becomes `--skip-video`, and the build proceeds through studio's **gated**
pipeline — `[1/5] Planning lesson ... all concepts covered`, not the legacy
ungated path.

Translation table, with `--lang` the only flag that needed a mapping:

| deprecated | replacement |
|---|---|
| `doc.md` positional, multi-file | unchanged |
| `-a` / `--audio` | `build --skip-video` |
| `-v` / `--video` | accepted, ignored — video is the default now |
| `--lang hi` / `mr` / `en` | `--narr-voice mhe-mix` / `mhe-mix` / `english` |
| `--voice` `--minutes` `--out` | unchanged |

`-v` is accepted rather than rejected, with a note, so an existing invocation
still runs instead of failing on a flag that became the default. Anything the
shim cannot translate raises rather than being ignored.

**README rewritten** so the documented command is the gated one. The old name now
appears only in the deprecation section, with the mapping table — the safety gap
was that following the docs reached the ungated pipeline.

### AK2. A mistake in my own shim, caught immediately

First end-to-end test failed with `unrecognized arguments: --skip-video`. My
first assumption was that `studio.main` ignored the argv list — **wrong**; it
accepts one. The actual cause was that my *test invocation* passed `--skip-video`,
a studio flag, to the legacy parser. Recorded because the wrong diagnosis was the
tempting one and would have sent me editing `studio.cli` for no reason.

### AK3. §AA split in two, as ruled — the ticket is the second half

The pause reduction is **not** treated as closing duration. Opened:

> **OPEN — 21.8 points of narration overrun (not the pause).** v012_015 is 137%
> of a 4-minute target: 15.4 points are structural silence, **21.8 are
> narration** — 505 words at a measured 103.6 wpm, where `LOUDNESS_WPM` predicts
> 103.0 (0.6% out, so the projection is not the cause). Reaching 240s needs
> ~415 words, **18% less narration**, or fewer scenes. This is a content
> decision about how much a 4-minute target should say, not a calibration bug,
> and no `LOUDNESS_WPM` tuning moves it.

Reducing `--pause` 3.0 → 1.5 recovers ~13.5s (5 of the 37 points) and is worth
doing on its own. It is **not** a fix for the 21.8.

Two preconditions recorded before the number is fixed: the 0.6s `CrossFadeIn`
was measured to sit at the head of each clip **over speech**, not inside the
pause (`video.py:703-728` — `d += pause` extends an image-only hold, the gap is a
separate silent `AudioClip`, the fade is applied to every clip's start), so the
gap stays a full 1.5s. And whether 1.5s *feels* rushed is perceptual — **listen
to two or three scene boundaries** before fixing the default.
