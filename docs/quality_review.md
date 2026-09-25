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
