# LLD — Narration repeat-safety & language-aware voice layer

Status: IMPLEMENTED (this session; reviewed by user) — see §7 rollout. QA sitrep:
`render`/`verify`/`review` on the fresh `--skip-video` build now report 0 repeated
narration phrases (before AND after), `voice: mhe-mix` persisted in plan.json.
Post-review adoption (see §8) added: protected-terminology allowlist,
sentence-aware safe repair, **deterministic narration rebuild fallback**,
hard "unrepairable repeat" resample gate, **hard pre-render gate** (banned
repeats block `render`/`build` with exit 1 + `*.rejected.audit.json`), audit +
idempotency in `verify`, schema/fingerprint persistence. Shipped plan
`output/mod03_gates2.plan.json` verifies as a true no-op (`idempotent: true`).
The v006 regression (`output/mod03_gates_v006.plan.json`: 4 banned repeats
survived their way to render) is now caught by the pre-render gate and
deterministically repaired by `verify` — 4 banned → 0, 3 unsafe scenes rebuilt.

---

## 1. Problem statement

Two coupled problems were found by QA on `mod03_gates` (and pre-exist on every
free-7B lesson):

1. **Repeated narration phrases (soft gate FAIL).** The review gate
   `repeating narration phrase` counts any >=3-word trigram that appears in
   >=2 scene narrations (`_narration_tail_repeat`). The free Qwen2.5-7B
   converges on the *same* filler/stall sentences in every scene:
   - a canned intro (`aaj aapne dekha ki ...`, `ha, kase samajhun?`,
     `kya hai ye?`, `ek example ke sath samajhunge.`),
   - it writes `is me` (Hinglish "isme") into every scene title → minus sound opens,
   - identical `design_decision` ("WHY THIS") text sourced into several scenes,
   - `_DD_LEADS` / `_POINTS_LEADS` rotation wraparound when scenes > pool size,
   - bullets rewriting takeaways verbatim (`near-duplicate bullets vs takeaways`).

2. **Hardcoded Hinglish/MHE constants.** Much of the spoken layer lives in
   module-level constants (see §2). If the narration language ever changes
   (pure English, Hindi, etc.), these constants silently stop matching and the
   entire de-dupe / language-check / spoken-decoration layer becomes ineffective —
   with no language switch anywhere to tell the engine what it may assume.

Goal: make **repeat-safety a language-agnostic, deterministic guarantee** and
make the **spoken layer an explicit, switchable "voice profile"** instead of a
sprawl of module constants.

---

## 2. Inventory of the hardcoded constants today

### 2a. Language-coupled (Hinglish / MHE spoken layer)

| Constant | Role | Coupled to |
|---|---|---|
| `_MHE_MARKERS` | token set feeding `NarrationLanguagePolicy.required_markers` → `narration_matches_voice_policy()` (advisory "not pure English") | narration language (MHE words) |
| `_TEMPLATE_AHEM` | regex that collapses the canned `ek ahem module hai...` skeleton — wrapped as `CleanupRule` inside `_MHE_POLICY` | 7B Hinglish output shape |
| `_TEMPLATE_SEEKHTE` | regex that collapses `chaliye seekhte hai kya ...` skeleton — wrapped as `CleanupRule` inside `_MHE_POLICY` | 7B Hinglish output shape |
| `_NARR_HEADS` | rotated heads when re-attaching template content | Hinglish narration |
| `_DD_LEADS` | spoken WHY-THIS lead-ins (size 8 post-fix) | Hinglish narration |
| `_POINTS_LEADS` | spoken bullet-list lead-ins (size 8 post-fix) | Hinglish narration |
| `_OPENER_POOL` | rotated scene openers (size 8) | Hinglish narration |
| `_CLOSER_POOL` | rotated rhetorical closers (size 4) | Hinglish narration |
| `yaad rakhein`, `Key takeaways,...`, `Sari takeaways...` | hardcoded strings in `_deepen_narrations` + deck build | Hinglish narration / deck label |
| `_DD_GENERIC` | degenerate design-decision phrases to drop | English dd text (doc terms) |
| `_ENUM_HINTS` | section-enum keyword hints (schema/registry/baseline...) | English doc keywords |

### 2b. Language-agnostic (token machinery — no words hardcoded)

| Piece | Behavior |
|---|---|
| `_nar_tokens` | the canonical tokenizer (NFKC, lower, strips `.,!?।"'` + curly quotes); the ONE tokenizer every repeat rule uses |
| `_nar_3grams_t` / `_nar_gram_key` | 3-token window extraction/keys for the gate + enforcer |
| `_narration_tail_repeat` → `_narration_repeat_report` | the gate itself; returns `(banned, protected)` — protected repeats never fail |
| `_protected_terms` / `_entity_tokens` / `_persist_protected_trigrams` | allowlist (takeaways, scene titles, dd, saved source trigrams, acronym/CamelCase/numeric/path tokens) that the enforcer never removes |
| `_drop_repeated_filler` | within-narration sentence dedup by trigram overlap |
| `_drop_shared_narration_sentences` | cross-scene whole-sentence dedup (>=3 token key) |
| `_enforce_unique_narration_trigrams` | **sentence-aware** repeat repair: wholesale-drop repeated-filler sentences (first occurrence preserved verbatim), trim real-content sentences only above `_NARR_MIN_TOKENS`, else mark `_narration_unsafe_scenes`; settles in <=4 passes |
| `_repair_unsafe_narrations` / `_rebuild_scene_narration` | **deterministic rebuild fallback**: a scene the enforcer could not repair (too short to trim safely / full duplicate) has its narration rebuilt from its OWN validated fields (title + design_decision + first distinct bullet) using only the voice's words; skipped pieces whose 3-grams collide with another scene, so the rebuild cannot invent a new banned repeat; hard pre-render gate keeps reporting whatever the rebuild could not fix |
| `_render_blocking_problems` | **hard pre-render gate**: recomputed from the plan's own content (banned repeats, unrepairable scenes, narration integrity, scene count). Called by `build` and `render` with exit 1 + `*.rejected.audit.json` — damaged narration NEVER reaches TTS/video, and no hardcoded Hinglish phrase list anywhere in Layer A |
| `_unsafe_repeat_scenes` | order-aware unsafe classifier for stored plans (full duplicate or too-short-and-repeating ⇒ HARD gate) |
| `_narration_integrity_problems` | rejects empty / <6-token / fragment-artifact narrations post-repair (HARD) |
| `_prune_bullet_takeaway_echo` | drop bullets whose jaccard >=0.78 with a takeaway |
| `_source_design_decisions` `used` guard | never re-source an already-used dd across scenes |
| `_deepen_narrations` spoken-dd guard + incremental `other_grams` | no dd re-spoken; no bullet/takeaway append that repeats another scene's narration; ends with `_clean_narration` |
| `_SOFT_PREFIXES` | soft→warn-only prefixes (`narration pure-English`, `repeating narration phrase`, near-dup bullets, placeholder titles, fused tokens). NOTE: `repeating narration phrase` may review as soft, but at the RENDER boundary it is re-derived by `_render_blocking_problems` and becomes a hard exit-1 gate — soft only for mid-plan diagnostics, never for shipping. |
| `_DEVANAGARI` | script detector (works for any Devanagari, not Hinglish-specific) |

> Key design fact: **everything in 2b works by comparing token windows, never
> by matching specific words**. The 7B filler problem (root cause of the QA
> failures) is therefore solved by 2b alone, in any language. The constants in
> 2a are only needed because the studio *writes its own* Hinglish sentences
> (openers/closers/leads) into otherwise English-plan narrations.

---

## 3. Design

### 3.1 Two-layer architecture

```
┌──────────────────────────────────────────────────────────────┐
│  LAYER A (language-agnostic, token-only)                     │
│  - repeat gate           (_narration_tail_repeat ->          │
│                           _narration_repeat_report)          │
│  - canonical tokenizer   (_nar_tokens: NFKC, lower,          │
│                           punct-strip) — ONE tokenizer       │
│  - protected allowlist   (_protected_terms: takeaways, scene │
│                           titles, dd, saved source trigrams, │
│                           entity tokens)                     │
│  - sentence sweeps       (filler, shared-sentence)           │
│  - sentence-aware enforcer(_enforce_unique_narration_trigrams│
│                           : wholesale-drops repeated filler  │
│                           sentences, trims real-content      │
│                           sentences only >= _NARR_MIN_TOKENS,│
│                           marks 'unsafe' otherwise)          │
│  - cross-scene guards    (dd, bullets, takeaways)            │
│  - bullet/takeaway prune (_prune_bullet_takeaway_echo)       │
│  - integrity checks      (_narration_integrity_problems)     │
│  - unsafe gate -> hard   (_unsafe_repeat_scenes)             │
│  These NEVER reference a word/script. Guarantee: hard gates  │
│  PASS for any whitespace-delimited narration language, or    │
│  the build resamples instead of shipping damage.             │
│                                                              │
│  LAYER B (selected VOICE profile; what the engine WRITES)    │
│  - NarrationVoice (+ NarrationLanguagePolicy, CleanupRule)   │
│  - used ONLY by: _assign_openers/_closers/_deepen,           │
│    _dedupe_narration_templates, narration_matches_voice_policy│
│    (advisory), takeaways label, cleanup rule patterns        │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 `NarrationVoice` + `NarrationLanguagePolicy` (new, replaces §2a constants)

Split per the review so a voice describes HOW to write while its policy
describes WHAT to check (two voices may share a policy):

```python
@dataclass(frozen=True)
class CleanupRule:
    name: str              # e.g. "mhe_ahem_skeleton" (diagnostics)
    pattern: re.Pattern    # template/stall scrub        (was _TEMPLATE_AHEM/...)

@dataclass(frozen=True)
class NarrationLanguagePolicy:
    expected_language: str          # "mixed" | "english" ...
    must_not_be_pure_english: bool  # False => pure English legal
    required_markers: frozenset[str]  # was _MHE_MARKERS
    min_marker_hits: int            # hits < this => advisory "looks pure English"
    cleanup_rules: tuple[CleanupRule, ...]

@dataclass(frozen=True)
class NarrationVoice:
    name: str                 # "mhe-mix" (default), "english"
    openers: tuple[str, ...]  # rotated scene openers  (was _OPENER_POOL)
    closers: tuple[str, ...]  # rotated closers        (was _CLOSER_POOL)
    dd_leads: tuple[str, ...] # spoken WHY-THIS leads  (was _DD_LEADS)
    points_leads: tuple[str, ...]  # spoken bullet leads (was _POINTS_LEADS)
    narr_heads: tuple[str, ...]    # template re-attach heads (was _NARR_HEADS)
    takeaways_lead: str            # " Key takeaways, yaad rakhein: "
    takeaways_any_lead: str
    takeaways_marker: str          # "yaad rakhein" (spoiler-free guard)
    policy: NarrationLanguagePolicy

    @property skeletons      # tuple(self.policy.cleanup_rules[i].pattern ...)
    @property skeleton_names
```

Module constants become default instances (the `_OPENER_POOL` etc. survive only
as the MHE definition source, never referenced directly by the pipeline):

```python
_MHE_POLICY  = NarrationLanguagePolicy(must_not_be_pure_english=True,
                                       required_markers=frozenset(_MHE_MARKERS),
                                       cleanup_rules=(CleanupRule("mhe_ahem_skeleton",
                                                                  _TEMPLATE_AHEM), ...))
_MHE_VOICE   = NarrationVoice(name="mhe-mix", openers=tuple(_OPENER_POOL), ...,
                              policy=_MHE_POLICY)
_ENGLISH_POLICY = NarrationLanguagePolicy(expected_language="english",
                                          cleanup_rules=(CleanupRule("en_understand_skeleton", ...), ...))
_ENGLISH_VOICE  = NarrationVoice(name="english", ... English texts ...,
                                 policy=_ENGLISH_POLICY)
_VOICES      = {"mhe-mix": _MHE_VOICE, "english": _ENGLISH_VOICE}
_voice_fingerprint(voice)  # sha256 over all voice texts -> persisted in plan.json
_make_voice(name)          # None -> _MHE_VOICE; unknown -> SystemExit(2)
```

### 3.3 Selection & threading

- CLI: `build`/`render`/`verify` gain `--narr-voice mhe-mix` (default; `--voice`
  remains the existing TTS voice). Unknown name → system exit 2 with the list
  of registered voices.
- `plan_lesson(content, minutes, voice)` → passes `voice` down to
  `_assign_openers`, `_assign_closers`, `_dedupe_narration_templates`,
  `_deepen_narrations`, `_scene_problem_map`, `guard_plan`, and the advisory
  language check `narration_matches_voice_policy(plan, voice)`.
- The language check is **advisory (soft)**: `must_not_be_pure_english` with
  fewer than `min_marker_hits` marker tokens reports `narration pure-English`
  and `review` lists it under `PASS (soft warnings)`; it never forces a
  whole-plan regen (the free 7B legitimately alternates pure-English technical
  sentences). `--narr-voice english` makes pure English legal for that voice.
- `verify`/`review` read `voice` from the saved plan.json (added to the JSON),
  so re-checking a saved plan uses the same spoken dialect.
- Plan.json persists repeat-policy provenance: `schema_version`,
  `repeat_policy_version`, `narration_voice` (name + sha256 fingerprint +
  language_tag), `protected_trigrams`. All JSON writes use `atomic_json_write`
  (tmp + fsync + same-dir rename), so a crash never leaves a truncated plan.

### 3.4 Repeat-safety guarantee (Layer A) — already in code, tested on mod03

Post-conditions enforced deterministically inside `plan_lesson` (REQUIRED to
hold before a plan is returned), in this order:

1. `_dedupe_plan_bullets` → exact-duplicate bullet removal (cross-scene).
2. `_prune_bullet_takeaway_echo` → bullets near-copying a takeaway are dropped
   (takeaway panel keeps the fact).
3. `_sanitize_design_decisions` → empty degenerate / >0.6-overlap dd fields.
4. `_source_design_decisions` → fill empties from docs, `used` guard keeps
   every sourced dd unique across scenes.
5. `_dedupe_narration_templates` =
   - `_drop_repeated_filler` (within-narration sentence dedup),
   - skeleton collapse via `policy.cleanup_rules` (AHEM/SEEKHTE) + re-attach
     under rotated `narr_heads`,
   - `_drop_shared_narration_sentences` (cross-scene, >=3-token key),
   - `_enforce_unique_narration_trigrams` (guarantee pass #1).  ← added
6. `_deepen_narrations` =
   - spoken-dd guard (never speak the same dd twice),
   - takeaways appended once per lesson, excludes grams colliding with any
     scene's narration (`incremental other_grams`),   ← fixed this session
   - **narration contract (this session):** the pipeline no longer reads
     bullets into narration at all (`voice.points_leads` + verbatim bullet dump
     removed). Spoken text must explain, not echo the slide (mentor A2.4); the
     slide carries the bullets, the narration carries the decision + takeaway.
     Ends with `_clean_narration`.
7. `_enforce_unique_narration_trigrams` (guarantee pass #2, AFTER deepen's
   appends).   ← added this session
8. `_repair_unsafe_narrations` (deterministic rebuild fallback, AFTER the
   enforcer): any scene still carrying a banned repeat (too short to trim /
   full duplicate) gets a narration rebuilt from its own validated fields
   (title + design_decision + first distinct bullet) using the voice's words;
   the enforcer re-runs over the whole plan so protected terms survive and only
   real repeats drop. If a scene still cannot be repaired it is left untouched
   and the hard pre-render gate reports it.   ← added this session
9. TOP-LEVEL guarantee (in `build` AND `render`): `_render_blocking_problems`
   recomputes the repeat gate from the plan content and refuses to render with
   exit 1 + `*.rejected.audit.json` — banned narration repeats never reach TTS
   or video.   ← added this session
10. Plan.json persistence: `schema_version`, `repeat_policy_version`,
    `narration_voice` (name+fingerprint+language_tag), `protected_trigrams`;
    text via `atomic_json_write`.   ← added this session

**Enforcer contract (sentence-aware, protected-aware):**
- Protected allowlist (`_protected_terms`) built from takeaways / scene titles /
  design decisions / saved `protected_trigrams` / entity tokens (acronyms,
  CamelCase, numerics, paths) is NEVER automatically removed; the gate reports
  such repeats separately (`protected terminology repeats`), never a failure.
- A repeated filler sentence (every word in a banned phrase) is dropped whole —
  only when it is NOT the plan-first occurrence (that original is preserved
  verbatim, untouched).
- A sentence that also holds real content is trimmed (banned phrase removed)
  only if at least `_NARR_MIN_TOKENS=12` tokens survive; otherwise the scene is
  marked `_narration_unsafe_scenes` and handed to `_repair_unsafe_narrations`
  for a deterministic rebuild from its own validated fields. If the rebuild
  clears the repeat gate the scene is repaired in-place (no LLM, no resample);
  only scenes that STILL carry a banned phrase become the HARD problem
  `unrepairable narration repeat (scene(s) ...)` → build resamples.
- After repair, `_narration_integrity_problems` rejects empty / <6-token /
  fragment-artifact narrations (hard). `_unsafe_repeat_scenes` recomputes the
  unsafe set on stored plans (first-occurrence-aware, order-scanned).
- **Pre-render hard gate:** `_render_blocking_problems` (banned repeats,
  unrepairable scenes, integrity, scene count) is checked at the start of
  `render` and right before the media render in `build`. A plan that fails it
  exits 1 with a `*.rejected.audit.json` and NO audio/video/PPTX is produced —
  so the v006 class of artifact (banned repeats surviving to render) is
  impossible going forward.

Measured on `mod03_gates`: repeat trigrams 44 → 0 target; near-dupe bullets → 0.

### 3.5 v007 free-7B endpoint remediation (this session)

The v007 build failed 5/5 plan samples with JSON-parse failures; replaying the
same bare call parsed 6/6, and the rejected window measured out as the culprit:
the whole-plan prompt (~18.4k chars) left only ~2,189 output tokens vs. the
1.5k–2.3k tokens a plan actually needs, so the hub truncated mid-object. Fixes:

| Change | Where | Effect |
|---|---|---|
| **Budget router** `choose_planner_budget(content, planner_prompt_for)` picks the WIDEST source window whose projected completion budget clears `PLANNER_MIN_OUTPUT` (2048 tokens), stepping the DOCUMENTS down through 12000→8000→6000→4000→2400; returns `(window, max_output_tokens)` fed straight to `ask_llm(..., max_tokens=...)`. | `llm.py` + `plan_lesson.build` | v007: 12000→2,354, 8000→**3,072** (was 2,189). v011_004 fix (§3.5.1): the 8000 stop point pinned dense docs to the 512-token clamp → ladder must clear 2048; gates doc now 2400-window→**2,772**. | 
| **Finish-reason capture** `_LAST_FINISH` + **truncation heuristic** `_looks_truncated` (unbalanced braces/brackets/parens, dangling ```/�) | `llm.py`, checked in `_parse_plan_json`/`_parse_scene_json` | A `"length"` finish or unbalanced JSON immediately raises → retry path, instead of a silent parse failure / ragged plan. |
| **Backoff+jitter** `_wait_before_retry(sample)` (1.5s→3s→6s→12s cap, ±25% jitter) | `util.py`, used between resamples in `cli.build` | Burstier 7B hubs stop being re-hammered right after a 429/parse failure. |
| **Trim whole-plan retries** `for _ in range(2)` → `range(1)` on the post-patch whole-plan rebuild (scene-patch rounds stay 2) | `plan.py` | Failures surface to the resample path faster; the gain now comes from routing, not from burning extra full-plan calls. |
| **Exactly-6-scenes grip** count text tightened to "exactly 6 scenes" (prompt + `count_grip`) | `config.py` + `plan.py` | Matches the 6-scene Perplexity sweet spot for 7B (5-8 range kept the model on the upper bound). |

#### 3.5.1 v011_004/005 completion-token bottleneck fix (this build epoch)

Dense 8-scene docs still died AFTER the 3.5 remediation: with the source at
8000 chars PLUS the full template + topic/outline/design blocks, the projected
completion dropped to the `planner_output_budget` clamp (512 tokens), so
`max_tokens=512` cut every plan mid-JSON (`finish_reason='stop'`,
`truncated_heuristic=True`, `len(raw)`≈2.0k chars — "all 5 samples failed to
parse; no plan produced"). The retry fallback re-fixed the window to 8000 chars
and recomputed the SAME 512-token budget, so it could never escape.

- **`_PLANNER_WINDOWS` = `(12000, 8000, 6000, 4000, 2400)`** —
  `choose_planner_budget` returns the WIDEST window whose projected budget
  clears `PLANNER_MIN_OUTPUT = 2048`; it no longer short-circuits at the 8000
  stop point with budget 512, and `planner_output_budget`'s clamp is retired
  for the full-plan call.
- **Output-side retry, not input-side re-shrink** — on a truncated /
  parse-failed plan, `plan_lesson.build` now drops the optional DESIGN CONTEXT
  block (pure framing; grounding survives via Phase-3 `source_chunk`) and
  re-probes the full ladder, so an output-cut is answered with real output
  budget.
- Measured on `08_concepts_mod03_gates` (17,975 chars, 8 scene items, real
  prompt construction): first call `window=2400 → max_tokens=2,772`; retry
  (design block dropped) `4000 → 2,375`. The plan frame alone is ~1.2k–2.2k
  output tokens, so mid-JSON truncation is out of reach.
- Live v011_005 proves it: planning now lands (topics, 8 scenes covering all
  source concepts, one patch round → 4 residual issues, partial-plan land,
  deterministic narration rebuild) instead of "all 5 samples failed to parse".

#### 3.5.2 tts_sentence_fragment hydration fix (this build epoch)

The planner fix unblocked planning, and landed v011_005 into the PRE-AUDIO
gate, which then correctly refused edge-tts: scene 2 sentence 7
'Compare the results against the baseline.' is a 6-word sentence with 4/6 of
its words in the scene title 'Baseline snapshot & compare — the core loop',
i.e. `_is_slide_fragment` (2-6 words + >=60% title-word overlap) — a
bare-title-echo class. Root cause: the deterministic narration rebuild
(`_rebuild_scene_narration`) speaks slide bullets BARE (`f"{bt}."`), and this
bullet's vocabulary overlaps its own scene title.

Bullets MUST stay verbatim in the spoken track (A2.13 '2+ spoken teaching
points' contract, `_has_teaching_claim`), so they cannot be dropped or
paraphrased away. Fix:

- **Layer B** — new `NarrationVoice.bullet_leads` pool (MHE: 5 phrases,
  English: 4; every lead >= 3 words). A >=3-word carrier dissolves the 60%
  threshold for every realistic fragment (6 words @ 5/6 overlap -> 5/9 = 0.56
  < 0.60); grammar stays in voice constants, not Layer A.
- **Speech layer (single point)** — `_hydrate_spoken_fragments` in
  `build_tts_script` prepends a rotating `bullet_leads` carrier to any sentence
  that would trip `_is_slide_fragment` (head sentence excluded, mirroring the
  gate). `audit_tts_script` reads `clip["spoken"]`, so ONE change covers every
  narration source (LLM pass, deterministic rebuild, verify repair).
  `test_audit_still_blocks_non_head_fragment` proves the gate itself stays a
  real guard for direct clip input.
- Live proof: stored `mod03_gates_v011_005.plan.json` re-audited deterministic
  → 0 FAILs (was 1), `_render_blocking_problems` empty; scene 2 now speaks
  'Iska matlab simple hai, Compare the results against the baseline.'

#### 3.5.3 SlideTextNotEnglish: non-Latin slide-text gate (this build epoch)

Audit of the shipped v011_006 deck found scene-1 Marathi ON the slides: 3
Devanagari bullets (`केंद्रभाग: गोल्डन सेट खालील कोड परीक्षण करणे`, ...) plus a
Devanagari analogy, all invented by the 7B (the source doc has ZERO Devanagari)
— yet the plan sailed every gate and the PPTX was built. Why it was invisible:

- `contains_corrupt_text` only flags MIXED scripts; PURE Devanagari → False.
- Grounding `_anchored` treats <2 content tokens as 'anchored by default';
  Devanagari has no Latin content tokens (`_WORD` is `[a-z0-9]+`) → never
  flagged ungrounded, never dropped ('dropped 1' in the build log was an
  unrelated Latin filler bullet).
- `guard_plan` / `_render_blocking_problems` returned `[]` → TTS QA PASS + PPTX.

Closure (all deterministic, mirrors the narration-corrupt precedent):

- **`text._has_non_latin_script`** — explicit non-Latin LETTER blocks (Indic,
  CJK/Kana/Hangul/fullwidth, Greek, Cyrillic, Hebrew, Arabic, Thai, Tibetan,
  Georgian, Armenian). Diagram arrows/box chars (`→ |`) stay Latin-safe.
- **`validate._slide_text_language_problems`** — scans EVERY slide-visible
  field per scene (`title`–`code_snippet`) + takeaways for non-Latin script;
  wired into BOTH `guard_plan` and `_render_blocking_problems`, so build,
  render and verify all fail honestly. Render-side fields not covered by the
  drop path (analogy/steps/flow/design_decision) are blocked here.
- **`_grounding_issues` / `_drop_ungrounded_slide_text`** — `len(tokens) < 2`
  → `not _has_non_latin_script(text)`: pure non-Latin text is ungrounded, so
  the deterministic drop removes it and the grounding hard gate flags it too.
- Live proof: stored v011_006 plan → `guard_plan` (2: bullets + analogy) and
  `_render_blocking_problems` (2) now both name `SlideTextNotEnglish`; the
  deterministic drop removes 3 of 4 leaked items and the hard gate keeps the
  plan rejected while the analogy survives the drop.

#### 3.5.4 Scene frame now scales with the concept count up to the proven 12-scene ceiling (this build epoch)

The prior `TARGET_MAX_SCENES = 8` forced `_concept_groups` to merge every doc's concepts into an 8-scene frame (`scene_target=8`). A 12-concept doc therefore lost concept 9 (the D5 two-lane design) to `_overflow_report.original_index: 9`. Root cause: the merge ceiling was an arbitrary 8, smaller than the doc's concept count.

Fix — `_concept_groups` now uses `AUTO_TRIM_MAX_SCENES` (12, the pipeline's proven 7B reliability ceiling) as its 1:1 merge ceiling instead of `TARGET_MAX_SCENES`:

- `_concept_groups(N concepts)` produces `min(N, 12)` groups: 1:1 when N ≤ 12 (every concept gets its own scene, including the D5 two-lane design), merged to 12 groups only when N > 12 (overflow report records the drops). `scene_target = min(len(concepts), 12)`.
- `_source_outline(content)[:AUTO_TRIM_MAX_SCENES]` and `_scene_count_problem` (concept-driven path) scale to the same 12 ceiling; `TARGET_MAX_SCENES = 8` remains the trim point for non-concept-driven plans.
- The 12-scene ceiling is not a new number — it is `AUTO_TRIM_MAX_SCENES`, already enforced as the absolute rejection cap (>12 rejected). The change only moves the 1:1 boundary up to that ceiling so concepts are no longer silently folded together.

The scene frame now genuinely adapts: a 9-concept doc emits 9 scenes, a 12-concept doc emits 12, a 15-concept doc merges to 12 with a loud overflow audit.

Verified post-review (this epoch): the stored v011_006 plan was trivially REJECTED by the new gates (`guard_plan`: 2 `SlideTextNotEnglish`, `_render_blocking_problems`: 2) and a re-import of the 12-concept Mod-03 doc returns `scene_target = 12` / `_concept_groups → 12` (live-checked). The external review's `plan_2.json` / `*_2.pptx` artifacts do not exist — the reviewed deck predates the fixes; the corrective action is a fresh rebuild, NOT a `scene_target = 9` config (the source doc has 12 concepts, and pinning 9 would re-introduce the silent drop class).

Mentor checklist decisions (locked after the Gemini "dynamic" review):

- **Scene budget** — concept-driven sizing with a hard 12 ceiling is ADOPTED. `MAX(DYNAMIC)`/`SPLIT_DECK`/`EXPAND`/`plan.config.max_scenes` REJECTED: no such schema field exists, and >12 must fail loudly (`_scene_count_problem`), never fragment decks. `TARGET_MAX_SCENES=8` retained as the fallback trim for non-concept-driven plans.
- **Diagrams** — prompt-level fix ADOPTED (`WHITEBOARD VISUALS`: labels must illustrate THIS slide's own topic; tolerance/threshold concepts must draw the comparison chain, e.g. `[Run Value] ---> [Delta vs Baseline] ---> [Absolute/Relative Threshold]`, never a baseline-pointer file). Renderer-class mapping REJECTED — `visual_diagram` is a text string parsed by `_parse_diagram`.
- **Slide richness** — prompt-level ADOPTED (`SOURCE-DRIVEN RICHNESS`): 2+ substantive bullets, generic single-word bullets REJECTABLE, schema/state concepts must emit the ACTUAL JSON keys (`{schema_version, baseline_id, path}`), path rules and structural fields — never paraphrase/placeholder. The Slide-8 one-word bullet class is a content defect, fixed in the prompt budget, not a layout-engine change.
- **Typography** — fixed 18pt/32pt retained; dynamic font-scaling formulas REJECTED (B1.4/B1.5 already pass; dynamic sizing risks contrast/readability regressions).
- **Voice/localization** — Hinglish preserved; `_sanitize_opening` widened to also strip period-less trailing `opening`/`closing`/`prompt_end`; runtime `detect_language`/`dynamic_translate` REJECTED (translating pure Hinglish would violate the MHE policy).
- **Grounding** — doc-grounded plain text only; runtime env-var interpolation (`${VAR}` placeholders) REJECTED as non-grounded hallucination.

### 3.6 Monolith → package split (this session)

`src/doc_to_video_tutor/studio.py` is now the `src/doc_to_video_tutor/studio/`
package: `config` → `util` → `text` → `llm` → `voice` → `narration` → `topics`
→ `plan` → `validate` → `slides` → `pptx` → `video` → `cli`, plus `__init__.py`
(re-exports every name, incl. all `_`-private names the tests touch) and
`__main__.py` so `python -m doc_to_video_tutor.studio` and the
`doc-to-studio` console script still resolve `main` from the package
`__init__`. `pyproject.toml` mypy override extended to
`doc_to_video_tutor.studio.*`. Verified: `ruff` clean, `mypy` clean on 16
files, 34/34 pytest green, `review`/`verify` on `mod03_gates_v006.plan.json`
unchanged (banned 4→0, VERDICT PASS) — a pure refactor (no behavior change).

### 3.7 Why layer-B constants stay per-language and never get guessed

The studio *writes* Hinglish narration (it is a feature of the requested
MHE-mix). We do not attempt to auto-detect the 7B's output language. Instead:
- the language is a **user-declared voice**, 
- every token-only safeguard in Layer A is **language-proof** regardless of
  that declaration,
- the only risk if the declared voice is wrong is aesthetic (wrong openers /
  English-gate lens), never a repeat-gate failure or a crash.

### 3.8 Files / symbols touched

| File | Change |
|---|---|
| `src/doc_to_video_tutor/studio/` (package) | split from the former `studio.py`: `config`/`util`/`text`/`llm`/`voice`/`narration`/`topics`/`plan`/`validate`/`slides`/`pptx`/`video`/`cli` + `__init__.py` + `__main__.py`; `main` still resolves from `doc_to_video_tutor.studio:main`; add `CleanupRule`/`NarrationLanguagePolicy`/`NarrationVoice`; thread `voice`; move §2a constants into `_MHE_VOICE`/`_MHE_POLICY` (+`_ENGLISH_VOICE`); add `--narr-voice` to `build`/`render`/`verify`; persist `voice` + `schema_version`/`repeat_policy_version`/`narration_voice`/`protected_trigrams` in plan.json via `atomic_json_write`; Layer-A items listed above; add `_repair_unsafe_narrations` + `_rebuild_scene_narration` (deterministic rebuild fallback, wired into `plan_lesson` AND `verify`) and `_render_blocking_problems` (pre-render gate in `build`/`render`, exit 1 + `*.rejected.audit.json`); v007 remediation `choose_planner_budget`/`planner_output_budget`/`_LAST_FINISH`/`_looks_truncated`/`_wait_before_retry` (done) |
| `docs/LLD-narration-voice.md` | this doc |
| (tests) `/tmp/opencode/voice_matrix.py` | matrix: Layer A vs English / MHE / Tamil must clear repairable banned phrases with zero hardcoded words; protected terms survive; verbatim duplicates classify `unsafe`; unknown voice exits 2 |

---

## 4. Testing before real-time render (the "fast loop" you asked for)

| Step | Command | Cost | What it proves |
|---|---|---|
| 1 | `python -m doc_to_video_tutor.studio verify <prefix>.plan.json` | seconds, no LLM/render | applies Layer-A fixers to a saved plan (incl. `_repair_unsafe_narrations` deterministic rebuild); prints before/after **banned** + **protected** repeat counts; self-checks idempotency (a second pass must not change narration); writes `*_fixed.plan.json` + `*.audit.json` atomically; exit 0 only if `guard_plan` passes AND no `_render_blocking_problems` |
| 2 | `python -m doc_to_video_tutor.studio review <prefix>.plan.json` | seconds | deterministic gates (banned narration repeats, on-topic, language) against saved topics/source bigrams; soft warnings are `PASS (soft warnings)`, hard problems exit 1 |
| 3 | `python -m doc_to_video_tutor.studio build <doc> --minutes 4 --out X --skip-video [--narr-voice mhe-mix]` | ~3 min (LLM+TTS+PPTX, NO video) | full plan gate chain + narration polish on a real 7B sample; unsafe scenes ⇒ deterministic rebuild first, only in-rebuild-survivors ⇒ hard gate ⇒ auto-resample; pre-render gate checked before media render |
| 4 | `python -m doc_to_video_tutor.studio render <X.fixed>.plan.json --out X` | ~12 min | only after 1–3 are green; re-checks `_render_blocking_problems` FIRST — a damaged plan exits 1 with `*.rejected.audit.json` (no partial media) |

Rule being adopted: **the ~12-min video render is the last step, never the
first** — anything that should fail fails at 1/2/3 in seconds/minutes.

---

## 5. Acceptance criteria

1. `review <plan.json>` returns `VERDICT: PASS` (exit 0) for the mod03 lesson
   after a fresh `--skip-video` build — with **zero** hardcoded stall/scrip
   scrubs added for it.
2. Layer-A passes are language-proof: same code path run over synthetic English,
   MHE, and a Devanagari/Tamil plan texts yields repeat gate PASS each time.
3. Selecting `--narr-voice english` produces openers/closers/leads in English
   and `narration_matches_voice_policy` uses the English policy (pure English
   legal); no crash, gates still deterministic.
4. Saved plan.json carries `voice` so `verify`/`review`/`render` re-use it.
5. `verify` never fails on a clean shipped plan: it is a true no-op
   (`idempotent: true`, zero narration/design-decision deltas) and writes an
   audit artifact. A verbatim-duplicate scene is repaired when redundant or,
   when it would gut a scene, rebuilt deterministically from its own validated
   fields; only scenes that survive the rebuild uncleaned are classified
   `unsafe` → hard gate → resample.
6. Protected terminology (takeaway/title/dd/source/entity trigrams) survives
   the enforcer and is reported separately, never a repeat-gate failure.
7. `uv run ruff check src/` and `uv run mypy src/` stay clean.
8. v18 final deliverables (`output/lld_tests_v18*`) remain untouched by these
   changes (verify/regression of a saved v18 plan.json).
9. **Pre-render gate:** a plan whose narration still carries a banned repeat is
   rejected by `render`/`build` with exit 1 + `*.rejected.audit.json` (verified
   on `output/mod03_gates_v006.plan.json`: 4 banned phrases → BLOCKED; `verify`
   then repairs it deterministically → 0 banned, `VERDICT: PASS`).

---

## 6. Decision record (the former open questions, resolved)

All five were approved by the user ("yes plz"); ADOPTED here as final:

1. **Voice scope → narration-only.** `voice` drives the *spoken* layer only;
   the deck stays English (labels/section names) as today. `deck_language` was
   a Perplexity follow-up, deferred.
2. **Register list → Python constant registry.** `_VOICES = {mhe-mix, english}`
   as constants; unknown name fails loudly (`SystemExit(2)`). TOML/JSON
   profiles were deferred.
3. **Default behavior → `--narr-voice` defaults to `mhe-mix`** (keeps today's
   behavior); a missing/invalid voice is a loud exit 2, never a guess.
4. **Enforcer aggressiveness → evolved past "auto-delete".** The final contract
   (implemented) is: protected terminology never removed; repeated filler
   sentences dropped whole (first occurrence preserved); real-content sentences
   trimmed only above `_NARR_MIN_TOKENS`; otherwise the scene is marked
   `unsafe` and the build resamples (hard gate). "Delete to zero no matter
   what" is explicitly OFF.
5. **English gate markers → policy-driven, advisory.** `narration_
   matches_voice_policy` uses the voice's `required_markers`/`min_marker_hits`;
   `english` voice has `must_not_be_pure_english=False` (pure English legal).
   Mismatch is a soft warning only — it never forces a whole-plan regen.

---

## 7. Rollout order

1. (done) Layer-A repeat machinery + dd/prune/leads fixes + `verify` command.
2. (done) `NarrationVoice` dataclass + `--narr-voice` threading (+ `english`
   voice registered) + persist voice in plan.json + LLM-flake resample guard in
   the `max_samples` build loop + enforcer hardened to match the gate's exact
   contract (lowercased tokens, within-scene repeats, cross-case).
   New-built `output/mod03_gates2.plan.json`: verify 0→0 repeats, review PASS.
3. (done) render `output/mod03_gates2.mp4` (191.4s) — QA recorded in
   `docs/quality_review.md` §H (SHIPPED-green).
4. (done) Implement Perplexity review adoption (below).

---

## 8. Perplexity review — adoption record

External review of the LLD produced 15 items; the "mentor think" pass accepted
these as adopted, deferred the rest, and the code now matches:

### Adopted (implemented)
| Recommendation | Implementation |
| --- | --- |
| Repetition risk: aggressive deletion mutilates narration | Enforcer is now **sentence-aware**: a repeated filler *sentence* (every word in a banned phrase) is dropped whole, but only when it is NOT the plan-first occurrence; a sentence with real content is trimmed only if at least `_NARR_MIN_TOKENS=12` tokens survive, else it is untouched and the scene is marked **unsafe**. |
| Safe repair instead of deletion | Protected-terminology allowlist: trigrams from takeaways / scene titles / design decisions / saved source trigrams / entity tokens (acronyms, CamelCase, numerics, paths) are **never** removed. The gate reports them separately (`protected terminology repeats`), never a failure. |
| Canonical shared tokenizer | `_nar_tokens` (NFKC, lower, strips `.,!?।"''`+curly quotes) is the only tokenizer for every repeat rule; hygienic case/within-scene comparisons match the gate. |
| Integrity checks + resample on unsafe scenes | New `_narration_integrity_problems` (empty / <6-token / artifact narrations) + `_unsafe_repeat_scenes` (full-duplicate or too-short-and-repeating). Either is a **hard** build problem → plan resample, never shipped broken. |
| Audit artifact + idempotency | `verify` writes `<stem>.audit.json` (before/after banned + protected + design-decisions dropped + idempotent flag) and self-checks a second pass leaves narration identical. |
| `NarrationVoice` / `NarrationLanguagePolicy` split | `NarrationVoice` is content-only; `NarrationLanguagePolicy` carries `expected_language`, `must_not_be_pure_english`, `required_markers`, `min_marker_hits`, `cleanup_rules` (`CleanupRule`). |
| Soft language gates | `narration_matches_voice_policy` is **advisory**: mismatch lists a soft warning, never forces a whole-plan regen (7B legitimately alternates pure-English technical sentences). |
| Schema / fingerprint persistence | plan.json stores `schema_version`, `repeat_policy_version`, `narration_voice` (name + sha256 fingerprint via `_voice_fingerprint`), `protected_trigrams`. Atomic tmp→rename JSON write (`atomic_json_write`) everywhere. |
| Documented scope | Tokenizer scope is explicitly whitespace-delimited narration languages/scripts (English / Hinglish / Devanagari Hindi–Marathi / Tamil-Kannada-Telugu-Malayalam prose); hyphens/slashes stay in-token. |
| Failed LLM patch must not silently keep invalid original | Patch-parse failures no longer leave a narration repeat in place silently: the whole-plan fix chain ends with `_repair_unsafe_narrations`, which deterministically rebuilds any still-unsafe scene's narration from its own validated fields. `verify` mirrors this chain, so a saved plan gets the same repair. |
| Banned narration repeats = hard pre-render gate | `_render_blocking_problems` (banned repeats / unrepairable scenes / integrity / scene count) is checked by `build` (before media render) and `render` (first thing): a failing plan exits 1 and writes `*.rejected.audit.json` — damaged narration can never reach TTS/video. Language-neutral: no Hinglish phrase list is hardcoded anywhere in Layer A (the four v006 offender phrases exist only as *test fixtures* + the MHE voice's own templates). |
| Deterministic repair instead of repeating LLM resamples | `_repair_unsafe_narrations` + `_rebuild_scene_narration` rebuild unsafe scenes from title + design_decision + first distinct bullet using the voice's words, skipping any fragment whose 3-grams collide with another scene. Only scenes that survive this uncleaned drive a whole-plan resample (keeps the free-7B budget). |

### Deferred (documented, not implemented)
Per-scene LLM resample prompt for unsafe scenes (resample is whole-plan;
scenes are instead repaired deterministically by `_repair_unsafe_narrations`);
lede/template rewrite tier; TOML voice profiles; `--tts-voice` rename of `--voice`;
`deck_language`; LLM-as-judge for language mix; non-whitespace segmenters.

### Implementation outcomes (verified 2026-09)
`uv run ruff check src/ && uv run mypy src/` clean. Language matrix
(`/tmp/opencode/voice_matrix.py`) PASS: repairable overlap clears banned in
English/MHE/Tamil (3→0), protected terminology survives and is reported not
banned, verbatim-duplicate scenes land in `unsafe` → hard resample gate, unknown
voice exits 2. `verify` + `review` on the shipped `output/mod03_gates2.plan.json`
are PASS and produce **zero** narration/design-decision deltas (true no-op,
idempotent, audit shows `idempotent: true`). Two latent bugs found and fixed
while verifying: double-period artifacts from deepen (now `_clean_narration` at
the end of deepen) and a case-sensitive repeat-guard that re-appended bullets
(now lowercased compare).

`uv run pytest tests/` 34/34 PASS after adding `_repair_unsafe_narrations` /
`_rebuild_scene_narration` / `_render_blocking_problems` tests (rebuild repairs
too-short repeating scenes from own fields; render gate blocks banned repeats
and out-of-range scene counts and passes clean plans). End-to-end on the v006
regression: `render output/mod03_gates_v006.plan.json` → BLOCKED exit 1
(`RENDER-BLOCKING: 4 banned narration phrase(s) unresolved: ['hai kya hai',
'hota hai kya', 'is module se', 'kya hai to']` + audit); `verify` → 4 banned → 0
(`deterministically rebuilt narration for 3 unsafe scene(s)`), `VERDICT: PASS`;
rendering the repaired plan passes the pre-render gate and produces sl+pptx.

---

## 9. Mentor production review checklist — reconciliation (single source of truth)

The external mentor checklist (lesson-plan/narration → slides → audio → video →
end-to-end) is adopted as the reviewers' contract. Every rule maps to a pipeline
hook or an explicit TODO. Status: **PASS** (implemented + verified),
**PARTIAL** (implemented but needs human/spot review or a narrower check),
**TODO** (documented known defect / next step).

### A. Lesson plan & narration contract
| Rule | Status | Hook / note |
|---|---|---|
| A1.1 scene count = source concept count (1:1) | PASS | concept-driven: one scene per extracted `##`/`###` concept (`_concept_headers`), `scene_target` contract honored by `_scene_count_problem`/`_render_blocking_problems`; legacy fallback 6 (5–8 range) when the doc has <5 or >12 headers (§14) |
| A1.2 every scene: title/narration/bullets/source_refs/topic | PASS | `guard_plan` schema validation |
| A1.3 source-progression order (no random reorder) | PARTIAL | source-frame ordering + on-topic patch; human spot-check |
| A1.4 no empty/placeholder/repeated-filler narration | PASS | `_narration_integrity_problems` (hard) |
| A2.1 zero unresolved non-protected repeated trigrams | PASS | Layer-A gate: `_narration_repeat_report` → `_enforce_unique…` → `_repair_unsafe…` → hard `_render_blocking_problems`; `verify` idempotent |
| A2.2 no slide-reading meta in narration ("slide pe", "exact points", …) | PASS | removed at source (`_deepen`/`_rebuild` no bullet dump) + spoken strip + audit `tts_slide_meta_leak` |
| A2.3 title mentioned ≤1× per scene's narration | PASS | `collapse_repeated_title` in `speech.py` + audit `tts_title_repeated` |
| A2.4 narration must not read bullets verbatim (explain, not echo) | PASS | narration contract (§3.4 step 6); optional future: Jaccard narration↔bullets ≤0.78 gate |
| A2.5 no fragment/incomplete punctuation/unbalanced quotes/brackets | PASS | `_clean_narration` + `repair_speech_punctuation` + integrity gate; `_DD_LEADS` are now colon-free grammatical connectives so spoken why-this text is not flattened into `Iska ahem reason Registry ...` |
| A2.6 narration word count 25–65 (warn 20–75) | PARTIAL | audit `tts_too_short` / `tts_too_long` warn only (min 25 / max ~70); CLI TTS QA now reports total words + spoken minutes vs `target_minutes` with under-run WARN when <70% (Gemini §12) |
| A2.7 no sentence >32 words | PASS | per-sentence gate in `audit_tts_script`: `tts_long_sentence` WARN when a single sentence exceeds 30 words (Gemini A2.7, §12) |
| A2.8 code/math tokens expanded (dot json, equals, slash) | PASS | `PronunciationRule` expansion in `speech.py` + audit `tts_code_fragment` |
| A2.9 no raw `M\d+` scene labels in spoken text | PASS | M1..M6 → "module one..six" expansion |
| A2.10 rhetorical filler removed/rewritten | PARTIAL | closers/openers de-rhetoric'd (statements); full filler gate is profile-local, TODO exhaustive |
| A2.11 script-integrity blockers (pre-TTS, no device call) | PASS | `audit_tts_script` second layer: `tts_voice_unset`, `tts_text_corrupted` (Devanagari+Cyrillic/Arabic/CJK mix, `\ufffd`, control), `tts_symbol_heavy` (`→`/slide-number/file residue), `tts_sentence_fragment` (title/bullet echo), `tts_unknown_token` (unexplained capitalized token not in title/source/glossary), `tts_too_short_critical` (<20), `tts_too_long_critical` (>90), `tts_required_concept_missing` — any FAIL → cli writes `*.rejected.tts_script.json`, exit 1, no edge-tts/MP3/MP4 (§13) |
| A2.12 reproducible voice persisted in plan/script | PASS | `_MHE_VOICE.tts_voice=hi-IN-SwaraNeural`, `_ENGLISH_VOICE.tts_voice=en-IN-NeerjaNeural`, `provider:"edge-tts"` recorded in `tts_script.json` + `plan.json.tts` (§13; fixes v009 `voice: None` registry gap) |
| A2.13 no transition-only/empty scene | PASS | plan-level `_has_teaching_content` (≥2 bullets, or steps/flow/analogy, or DD, or narration content-density ≥0.4) + clip-level `tts_required_concept_missing` (§13) + `_repair_thin_narrations`: deterministically rebuilds stubbed/title-echo-only narration from the scene's own bullets+DD under `verify` (A2.14) |
| A2.14 spoken-title head is not a fragment | PASS | first spoken sentence equal to `spoken_title` is the deliberate title mention (spoken once) and is exempt from `tts_sentence_fragment`; later title echoes still FAIL (§13 v011) — see §9-A2.14b/c |
| A3.1 ≥1 valid source_ref per scene | PASS | `guard_plan` source_refs validation |
| A3.2 all required topics covered | PASS | on-topic coverage → targeted patch → whole-plan resample |
| A3.3 no ungrounded major concepts | PASS | ungrounded slide-text gate (`guard_plan` assertion failures) |

### B. Slide (PPTX) visual design
| Rule | Status | Hook / note |
|---|---|---|
| B1.1 ≤5–7 lines per content slide | PARTIAL | `_VIDEO_BODY_MAX` overflow guard caps height; explicit line-count gate TODO |
| B1.2 ≤4 bullets, 3 preferred | PASS | `schema.SlideScene.bullets` `max_length=4`; `_paginate_plan_slides` splits at 4/page; `_scene_metadata_problems` HARD-FAILS any `bullet_pages` whose flattened content differs from `bullets` (v011_017 stale-page class) |
| B1.4 body text ≥18pt (20–24 preferred) | PASS | scene bullets = 18pt; **Key-takeaways bullets = 18pt** (Gemini §12: 16pt → 18pt, `pptx.py` `_ppt_para`) |
| B1.5 title ≥32–36pt | PASS | **slide title = 32pt bold** (Gemini §12: 24pt → 32pt, `pptx.py._ppt_slide_chrome`; ≤44 chars to stay one line above the divider) |
| B1.6 ≤2 font families | PASS | Arial (+ Courier New for code panels only) |
| B1.7 legible sans-serif body | PASS | Arial |
| B2.1 strong text/bg contrast | PASS | dark template (bg `(18,24,38)`, body white, accent blue `(66,133,244)`, gold `(255,193,7)`) |
| B2.2 no vibrating color pairs | PASS | accent/gold on dark only; no red-on-green |
| B2.3 no text over busy images | PASS | flat solid backgrounds |
| B2.4 consistent color semantics | PASS | single `ACCENT/GOLD/MUTED` palette in `config.py` |
| B2.5 no bold blocks behind text | PASS | solid dark fill behind all text |
| B3.1 one idea per slide | PARTIAL | human content review |
| B3.2 key terms emphasized (bold/color), not overused | PARTIAL | bold titles/section + gold divider; spot-check |
| B3.3 headings/subheadings for long text | PARTIAL | chrome provides section + title; ok for deck |
| B3.5 diagrams simplified | PARTIAL | `_parse_diagram` + `_ppt_diagram` render node boxes/GOLD chevrons (never raw `--->` text); `_ensure_technical_visuals` now adds deterministic registry/metric-kind fallbacks from `source_chunk`; human content review still required |
| B4.1 margins/grid consistent | PASS | shared `_ppt_slide_chrome` template |
| B4.2 title placement/style consistent | PASS | chrome mirrors the video header |
| B4.3 bullet style consistent | PASS | `•` (Arial) via `_ppt_bullet` |
| B4.4 generous whitespace | PARTIAL | overflow guard keeps slides uncrowded; spot-check |
| B4.5 takeaways only from retained scenes | PASS | takeaway panel built from plan's `takeaways` (source-grounded) |

### C. Audio (TTS) quality
| Rule | Status | Hook / note |
|---|---|---|
| C1.1 one consistent neural voice per lesson | PASS | single `edge_tts` voice threaded through all clips |
| C1.2 voice matches narration language profile | PARTIAL | default `hi-IN-SwaraNeural`; **voice bake-off pending** (en-IN-Neerja/Prabhat, hi-IN-Swara/Madhur) — next step |
| C1.3 speaking rate −4%…−12% | PASS | `TTS_RATE=-8%` default, CLI `--rate` |
| C1.4 pitch/volume near default | PASS | `TTS_PITCH=+0Hz`, `TTS_VOLUME=+0%` |
| C2.1–C2.4 intelligibility/pacing | PARTIAL | rate −8% + punctuation repair + pronunciation dict; needs listening pass on term list |
| C2.5 no TTS artifacts/glitches | PASS | raw `edge_tts` output; no post-processing |
| C3.1 consistent loudness across clips | PARTIAL | **ffmpeg `loudnorm` one-pass applied in `synth_scenes`** (`video._normalize_loudness`, `TTS_LOUDNORM=on`) — measured via `ebur128` in tester L2; silent-fallback keeps originals if ffmpeg fails (Gemini §12) |
| C3.2 target ≈ −16 LUFS integrated | PARTIAL | `loudnorm I=-16` (`LOUDNESS_TARGET`); PASS follows ebur128 measurement |
| C3.3 true peak ≈ −1.5 dBTP, no clipping | PARTIAL | `loudnorm TP=-1.5` (`LOUDNESS_TP`); PASS follows ebur128 measurement |
| C3.4 no >1.5s silence gaps within a scene | PARTIAL | native TTS pauses; not measured |
| C3.5 longer leading/trailing pause on takeaways | PASS | `end_hold=6.0s` after final clip |

### D. Video
| Rule | Status | Hook / note |
|---|---|---|
| D1.1 slide shown ≥ narration duration | PASS | slide duration = `max(audio.duration, 0.1)` + 3.0s inter-scene pause |
| D1.2 no sub-1.5–2s slide flashes | PARTIAL | per-bullet intro split can shorten a variant; not gated |
| D1.3 simple consistent transitions | PASS | uniform 0.6s `CrossFadeIn` |
| D1.4 takeaways holds ~2s after audio | PASS | `end_hold=6.0s` |
| D2.1 ≥720p (1080p preferred for detail) | PARTIAL | 1280×720 @24fps; 1080p not offered |
| D2.2 sharp readable text | PASS | 720p CBR encode; desktop check |
| D2.3 no compression artifacts | PARTIAL | CBR `700k` `nal-hrd=cbr` (above QA floor); complex slides spot-check |
| D2.4 16:9, no distortion | PASS | 1280×720 canonical |
| D3.1–D3.5 teaching effectiveness | PARTIAL | qualitative; reviewer spot-checks |

### E. End-to-end sanity
| Rule | Status | Hook / note |
|---|---|---|
| E1.1 plan version, voice, TTS settings in metadata | PASS | plan.json has `schema_version`/`narration_voice` and now `tts {voice,rate,pitch,volume}` |
| E1.2 exact TTS script as an artifact | PASS | `{out}.tts_script.json` + `{out}.script.txt` + persisted `{out}_audio/scene_*.mp3` |
| E1.3 20–30s voice bake-off before full render | TODO | **next step**: scenes 2/4/6 spoken text × 4 voices → `output/voice_bakeoff/` |
| E1.4 known defects logged | PARTIAL | §10 TODO list below is the running log |
| E1.5 `verify` idempotency | PASS | verified every session (`idempotent: true`, zero deltas on shipped plans) |

---

## 10. Session delta — spoken layer, narration contract, and hard-gate drill (this session)

### Spoken/TTS layer (new)
- **`src/doc_to_video_tutor/studio/speech.py`** (new): spoken normalizer + audit.
  - `speech_expand` — longest-first, word-boundary alternation over
    `pronunciation_rules` (M1..M6→"module one..six", Active.json/plan.json/
    `.json`→"dot json", `.yml`, `llm_eval_gate.yml`, `=`→equals, `&`→and,
    `+`→plus, `1/(n+1)`→"one over n plus one", …).
  - `strip_slide_meta` — removes profile `spoken_meta_leaks` phrases
    ("slide pe", "exact points", "points likhe hain", "main points", …).
  - `collapse_repeated_title` — enforces A2.3 (title ≤1 spoken mention).
  - `repair_speech_punctuation` / `_flatten_parentheses` — replaces
    `...`, collapses stray quotes, orphaned `:`/`-`, unbalanced spans.
  - `build_tts_script(plan, voice)` → `{"voice", "rate", "pitch", "volume",
    "clips":[{"role","index","title","narration","spoken_title","spoken",
    "word_count"}]}`; every clip's `spoken` is a fully expanded, de-meta'd,
    punctuation-clean single-voice string ready for `edge_tts`.
  - `audit_tts_script` — FAIL/WARN findings: residual slide-meta leak,
    `M\d+`/code fragments, title repeats, too-short (<25) / too-long (>70).
- **`NarrationVoice`** gained `tts_voice`, `rate`, `pitch`, `volume`,
  `pronunciation_rules`, `spoken_meta_leaks` (both `_MHE_VOICE` / `_ENGLISH_VOICE`;
  fingerprint covers them). `PronunciationRule` + `_MHE_TECH_PRONUNCIATION` /
  `_ENGLISH_TECH_PRONUNCIATION` + `_MHE_LEAK_PHRASES` / `_EN_LEAK_PHRASES`.
- `config.py`: `TTS_RATE="-8%"`, `TTS_PITCH="+0Hz"`, `TTS_VOLUME="+0%"` env
  defaults; `_CLOSER_POOL` de-rhetoric'd (statement answer-beats, no "kya hai?").
- CLI (`build`/`render`): `--voice/--rate/--pitch/--volume`; `build_tts_script`
  + `_write_tts_artifacts` (writes `{out}.tts_script.json`, `{out}.script.txt`,
  prints TTS QA report) before media; `video.synth_scenes(script, …)` synthesizes
  from `clips[].spoken` with rate/pitch/volume; per-scene clips persisted to
  `{out}_audio/scene_*.mp3`. Plan.json now stores `tts {voice,rate,pitch,volume}`.

### Pipeline bug fixes (caught by the hard gate, both deterministic)
1. **`_opener_module_name` ("Is 01 me data" → "Is me data")** made EVERY scene
   opener contain the same "hain is me" trigram → guaranteed cross-scene banned
   repeat. Now skips leading `is/me/numeric` tokens so openers carry the real
   topic words ("data testset", "metric registry") — the exact v008 blockers
   (`['hai kya hai','kaam karta hai','karta hai kya']`, then `['the verdict
   engine']`) were exposed precisely because the pre-render hard gate refused
   them; the later draws repaired to `banned after: []`.
2. **`_rebuild_scene_narration`** promised "title + design_decision + first
   distinct bullet" (repeated in the LLD since §3.4) but only emitted
   opener + dd, so scenes the enforcer marked unsafe (trim < `_NARR_MIN_TOKENS`)
   could not be repaired. Now really appends a collision-checked title, dd lead,
   and up to 3 distinct bullets until ≥ `_NARR_MIN_TOKENS` — matching its
   docstring. Verified: `_repair_unsafe_narrations` clears the blocked plan
   (`banned before: ['the verdict engine']` → after: `[]`, 2 scenes rebuilt).

### Mentoring evidence
- Fresh `mod03_gates_v008` build: **green end-to-end** (6 scenes, `review`
  VERDICT PASS, pre-render gate passed). TTS QA: `slide-meta leaks: 0`,
  2 WARN (`tts_title_repeated` scene 3, `tts_too_short` scene 4 — 18 words).
- Mentor package produced (deterministic `render`, no LLM):
  `output/mod03_gates_v008m.plan.json` · `.pptx` · `.tts_script.json` ·
  `.script.txt` · `output/mod03_gates_v008m_audio/scene_01..07.mp3`
  (123 s spoken ≈ target-tracking note: narration runs ~2.2 min vs 4.0 target — a
  7B-short-utterance artifact; A2.6/duration is the open MAJOR).
- Tests: `test_speech.py` (7) added; **41/41 PASS**, `ruff` + `mypy` clean.
- Known defects / next steps (mentor E1.4 log): measured ebur128 PASS pending;
  E1.3 **voice bake-off**
  (`en-IN-NeerjaNeural`, `en-IN-PrabhatNeural`, `hi-IN-SwaraNeural`,
  `hi-IN-MadhurNeural`, all −8%/+0Hz/+0%, scenes 2/4/6 → `output/voice_bakeoff/`);
  A2.4 optional Jaccard gate; full video render of the
  final voice choice. C3.1–C3.3 loudness, B1.4 takeaways 18pt, B1.5 title 32pt,
  and A2.7 sentence-length gate are now implemented (§12).

---

## 11. Latest Perplexity review (audio/video/pptx generation) — adoption record

External review of the audio/TTS layer (script quality, voice selection,
loudness) plus the mentor's consolidated production checklist (slides B, audio
C, video D, end-to-end E) is reconciled below so the mentor/tech-lead can see
at a glance which recommendations were **adopted**, **partially adopted**,
**pending**, or **deferred**. §9 carries the per-rule status; §10 the code
delta. Anything marked ADOPTED must not be re-suggested.

### Audio / TTS (from the recent Perplexity audio review)
| Recommendation | Status | Where |
|---|---|---|
| Separate visual from spoken (narration contract; explain, don't echo bullets) | ADOPTED | §3.4 step 6; `_deepen`/`_rebuild` stop dumping `points_leads` + verbatim bullets |
| Exact TTS script as an auditable artifact (`tts_script.json` + `.txt`) | ADOPTED | `speech.build_tts_script` + `cli._write_tts_artifacts` (E1.2) |
| Expand code/math tokens for speech (`.json`→dot json, `=`→equals, `&`→and, `+`→plus, `1/(n+1)`→one over n plus one) | ADOPTED | `speech.speech_expand` (A2.8) |
| Pronunciation dictionary for technical terms | ADOPTED | `voice.PronunciationRule` + `_MHE_TECH_PRONUNCIATION`/`_ENGLISH_TECH_PRONUNCIATION` (A2.4/C2.4) |
| No slide-metadata / bullet-listing phrases in spoken text ("slide pe", "exact points") | ADOPTED | source removal + `speech.strip_slide_meta` + `_MHE/_EN_LEAK_PHRASES` + audit `tts_slide_meta_leak` (A2.2); v008 = 0 leaks |
| Title mentioned at most once per scene narration | ADOPTED | `speech.collapse_repeated_title` + audit `tts_title_repeated` (A2.3) |
| Remove rhetorical filler ("kya hai?", "samjho isse" closers) | ADOPTED | `_CLOSER_POOL`/EN openers → statement answer-beats (A2.10); exhaustive per-profile filler gate DEFERRED |
| Voice bake-off before full render (`en-IN-Neerja/Prabhat`, `hi-IN-Swara/Madhur` @ `-8%/+0Hz/+0%`) | PENDING | E1.3 — next step → `output/voice_bakeoff/` |
| Rate/pitch/volume profile (`-8%/+0Hz/+0%`) | ADOPTED | `config.py` `TTS_*` defaults + CLI `--rate/--pitch/--volume` + `edge_tts.Communicate` (C1.3/C1.4) |
| Loudness normalization ≈ −16 LUFS, true peak ≤ −1.5 dBTP | ADOPTED | C3.1–C3.3: `video._normalize_loudness` (ffmpeg `loudnorm` one-pass, `TTS_LOUDNORM=on`, env-tunable targets), silent-fallback keeps originals if ffmpeg fails (Gemini §12) |
| TTS quality gates (slide-meta, code fragments, word-count bands) | ADOPTED | `speech.audit_tts_script` FAIL/WARN + CLI TTS QA report (A2.2/A2.6/A2.8/A2.9); per-sentence >32-word gate ADOPTED (`tts_long_sentence` WARN >30 words, A2.7) |
| Natural pacing / pauses | PARTIAL | 3.0 s inter-scene silence + 6.0 s takeaways end-hold in `assemble_video`; intra-scene silence insertion DEFERRED |
| TTS settings in plan metadata | ADOPTED | plan.json `tts {voice,rate,pitch,volume}` + `narration_voice` (E1.1) |
| Audio clips persist for external review | ADOPTED | `{out}_audio/scene_*.mp3` copied from the temp render dir (mentor listens to the exact clip that ships) |

### Video (Perplexity/mentor D-rules)
| Recommendation | Status | Where |
|---|---|---|
| Slide stays on screen ≥ narration clip duration | ADOPTED | slide duration = `max(audio.duration, 0.1)` + pause (D1.1) |
| ≥720p, 16:9, sharp readable text, no compression artifacts | ADOPTED | 1280×720 @24fps CBR `700k` `nal-hrd=cbr` (D2); 1080p DEFERRED |
| Simple consistent transitions + silent reflection hold on takeaways | ADOPTED | uniform 0.6 s CrossFadeIn, `end_hold=6.0 s` (D1.3/D1.4); no sub-1.5 s flash gate (D1.2 PARTIAL) |

### PPTX / slides (Perplexity/mentor B-rules)
| Recommendation | Status | Where |
|---|---|---|
| Dark-theme template conformance | ADOPTED | BG(18,24,38)/PANEL(30,40,60)/ACCENT #4285F4/GOLD/GREEN/MUTED chrome; graphic-reviewer enforces (B2/B4) |
| ≤2 font families, legible sans-serif | ADOPTED | Arial body + Courier New code-only (B1.6/B1.7) |
| Strong contrast, no vibrating pairs, consistent color semantics | ADOPTED | white/gold on dark; single palette in `config.py` (B2.1/B2.2/B2.4) |
| Body text ≥ 18pt | PASS | scene bullets 18pt; **Key-takeaways bullets bumped 16pt → 18pt** (B1.4, Gemini §12) |
| Slide title ≥ 32–36pt | PASS | **title bumped 24pt → 32pt bold**, ≤44 chars to stay one line above the divider (B1.5, Gemini §12) |
| ≤4 bullets / ≤5–7 lines per slide, real bullet chars | ADOPTED | overflow guard + `a:buChar` bullets; hard line-count gate TODO (B1.1/B1.2) |
| Takeaways summarize only retained scenes | ADOPTED | takeaway panel from plan `takeaways` (B4.5) |
| Consistent margins/title/bullet chrome, whitespace | ADOPTED | shared `_ppt_slide_chrome` (B4) |

### Evidence from this session
- Two consecutive v008 attempts were BLOCKED by the A2.1 pre-render hard gate
  (`['hai kya hai','kaam karta hai','karta hai kya']`, then `['the verdict
  engine']`) — exactly the reader role the mentor wants (never ship damaged
  narration). Both were pipeline-self-inflicted and fixed deterministically
  (see §10: opener module-label bug, rebuild title/dd/bullet fallback).
- Fresh v008 build went GREEN end-to-end; `review` `VERDICT: PASS`; TTS QA
  0 slide-meta leaks; mentor package `output/mod03_gates_v008m.*` +
  `_audio/` ready for the audio/Vfx reviewer.
- The QA (tester) and graphic-reviewer agents in `opencode.json` now encode the
  checklist above (L1/L2 gates + B/D rules) per §9/§11.

---

## 12. Latest Gemini architecture review — adoption record

External review of LLD-narration-voice praised the architecture (deterministic
rebuild fallback over stochastic re-sampling, hard pre-render gate, budget-aware
token router, clean script-normalization decoupling in `speech.py`), then listed
four technical gaps. Reconciliation below; anything ADOPTED is implemented and
must not be re-suggested. Status column tracks §9.

### Gemini gap 1 — unnormalized audio signal path (C3.1–C3.3)
| Item | Verdict | Resolution |
|---|---|---|
| Raw `edge_tts` clips concatenated without loudness matching | ADOPTED | `video._normalize_loudness`: one-pass ffmpeg `loudnorm` `I=-16:TP=-1.5:LRA=11`, 44.1 kHz mono MP3, applied to every clip in `synth_scenes`; env-tunable `LOUDNESS_TARGET`/`LOUDNESS_TP`, kill-switch `TTS_LOUDNORM=off`. Recorded 1/N clip in the TTS line. |
| No volume jumps between consecutive scenes | ADOPTED | same normalization across all scene + final clips before full-audio concat in `assemble_video` |
| Use `ffmpeg-python` package | NOT ADOPTED | the `ffmpeg` python wrapper is not a project dependency; implemented with stdlib `subprocess.run([ffmpeg, ...])` + `shutil.which` fallback — zero new deps |
| Silent degrade, never destroy TTS | ADOPTED | on ffmpeg absence/failure the original file is kept and a WARN printed |

Status: §9 C3.1–C3.3 PARTIAL → measured `ebur128` PASS is the tester's L2 gate
(§9 note); true-peak/`-16 LUFS` target configurable in `config.py`.

### Gemini gap 2 — narration duration deficit vs target (A2.6 / E1.2)
| Item | Verdict | Resolution |
|---|---|---|
| v008m: 123 s spoken (~2.0 min) vs 240 s (4.0 min) target | CONFIRMED (MAJOR) | §10 logs it; `LOUDNESS_WPM=135` duration model adds a measured narration line to the CLI TTS QA report |
| Implement target word-count check during plan validation | PARTIAL (see note) | CLI now prints total spoken words + estimated spoken minutes vs `target_minutes` (`script["target_minutes"]`) with a WARN when <70% of the 4.0-min target |
| Trigger explicit context-grounded expansion prompt when <~400 words | DEFERRED | deterministic `_rebuild_scene_narration`/`_deepen_narrations` (title/dd/≤3 bullets) is the current counter-measure; an LLM re-prompt risks regenerating repeat-pattern attractors and is deliberately NOT used as a first-line fix — revisit once tech-lead flags duration as blocking |

Status: §9 A2.6 PARTIAL; the WARN is a signal, not a hard gate (quiet MAJOR
logged at §10).

### Gemini gap 3 — PPTX typography hierarchy (B1.4, B1.5)
| Item | Verdict | Resolution |
|---|---|---|
| Slide title currently 24pt bold | ADOPTED | `pptx._ppt_slide_chrome`: 32pt bold, title capped at 44 chars so it never wraps onto the gold divider at y=1.28 |
| Takeaway bullets currently 16pt | ADOPTED | 18pt in the scene + final takeaway slides; all body text ≥18pt |
| Floor all body/takeaway at 18pt | ADOPTED | scene bullets 18pt (already), takeaways 18pt, gold "Why this"/Analogy/Diagram lines bumped 16pt → 18pt, opening text 14pt → 18pt |

Status: §9 B1.4 / B1.5 now PASS; the deck matches the video's visual hierarchy.

### Gemini gap 4 — whitespace tokenization boundary risk (Layer A)
| Item | Verdict | Resolution |
|---|---|---|
| Layer A canonical tokenizer (`_nar_tokens`) relies on whitespace split | CONFIRMED & DOCUMENTED | the scope note (§3 text.py docstring) already limits Layer A to whitespace-delimited scripts (English, Hinglish, Devanagari, Tamil/Kannada/Telugu/Malayalam) |
| Add explicit guard / flag non-space-delimited scripts | ADOPTED | `text._UNSEGMENTED` regex (Han/Kana/Hangul/fullwidth) — `_nar_tokens` WARNs once when an unsegmented script is detected, so trigram-window degeneracy is surfaced instead of silently producing false repeats |

Status: §3 documented; runtime WARN added — no behavior change for the shipped
whitespace-delimited languages.

### Net new action items from this review
- P1 (implemented this session): loudness normalization; deck typography; narration
  under-run WARN in CLI TTS QA; A2.7 sentence-length gate (existed as
  `tts_long_sentence` — now reconciled to PASS); Layer-A unsegmented-script guard.
- P2 (pending): E1.3 voice bake-off unchanged (`en-IN-NeerjaNeural`,
  `en-IN-PrabhatNeural`, `hi-IN-SwaraNeural`, `hi-IN-MadhurNeural` @
  `-8%/+0Hz/+0%`, scenes 2/4/6 → `output/voice_bakeoff/`).
- Measurement backlog: run tester L2 ebur128 to flip C3.1–C3.3 to PASS; then the
  4-voice bake-off; then full video render of the final voice.

## 13. Latest v009 review — adoption record (narration integrity)

Reviewed `output/mod03_gates_v009.script.txt` (external review panel + a Gemini
mentor review, same root causes). Every defect was **reproduced deterministically**,
then fixed at the generator + gate level.

### Confirmed on v009 (before fix) and now blocked
| Claimed defect | Verification | Blocker that now fires |
|---|---|---|
| `tts voice: None`, unreproducible audio | `plan.json.data.tts.voice=None`; `_MHE_VOICE.tts_voice` unset (default `None`) | A2.12 (`tts_voice_unset`) |
| Scene 3 = 12-word transition only ("Concept by concept…") | narrated 12 words, title echo only | `tts_too_short_critical`, `tts_sentence_fragment`, `tts_required_concept_missing` |
| Scene 4 raw `measurement→decision framing`, "1 Baseline snapshot…" | raw `→` + numbered title restated as narration | `tts_symbol_heavy`, `tts_sentence_fragment` |
| Scene 1 hallucinated `Prahlad` + `IS` shout + title echo | `Prahlad, prakriya samajh aata hai` with no source anchor | `tts_unknown_token` |
| Scene 2 114-word info dump | 114 spoken words listing 5 concepts | `tts_too_long_critical` |
| Scene 5 75-word + broken opener | 75 words, "Aaj hum samajh sakte ho" | `tts_sentence_fragment` / `tts_too_long` |
| Scene 6 Devanagari+Cyrillic corruption | `गेट एक ठोस बट्याड़ है, गेडरेनल एक мян-मान…` | `tts_text_corrupted` + plan-level corrupt-text gate |
| Takeaways run-on | tiles joined with a bare space, no sentence split | `_join_sentence_items` → each bullet gets `"."` |

### What was implemented (all tests green: 49 pytest, ruff, mypy)
1. **Concrete voices** — `voice.py` `_MHE_VOICE.tts_voice="hi-IN-SwaraNeural"`,
   `_ENGLISH_VOICE.tts_voice="en-IN-NeerjaNeural"`, `provider:"edge-tts"` persisted
   in `tts_script.json` and `plan.json.tts`.
2. **Second-layer audit** (`speech.audit_tts_script`) — new blocker codes under §9/A2.11
   feeding a hard pre-audio gate in `cli._tts_blocking_findings`. Any FAIL → before
   Edge TTS the CLI writes `*.rejected.tts_script.json` + audit, prints a verdict,
   `exit 1` (build **and** render paths).
3. **Pronunciation/machinery** — `→`→" to ", `←`→" from ", `;`→", " expansion;
   takeaways joined as sentences ("…."); `collapse_repeated_title` unchanged
   (already drops every repeat after the first).
4. **Standalone openers** — `_OPENER_POOL` no longer injects the verbatim English
   slide title into a Hinglish opener (the "…ke baare mein - framework" construction
   is gone); Python's `.format(m=…)` remains a no-op so callers don't change.
5. **Prompt constraints** (`config.STUDIO_PROMPT`) — **Devanagari banned** (Latin/Roman
   Hinglish only, killing the script-corruption root cause); narration = 30–55 spoken
   words, 2–4 complete sentences, **one concept per scene**, title ≤1 mention, number
   prefixes spelled ("first"/"step one", never "1:"), no raw symbols, no rhetorical filler.
6. **Plan-level guards** (`validate._narration_integrity_problems`) — corrupt/mixed
   script in narration blocks render; `_has_teaching_content` rejects transition-only
   scenes (bullets/steps/flow/DD surrogate or content-density rule).

### Evidence
`doc-to-studio render output/mod03_gates_v009.plan.json --out v009 --out-dir /tmp/v009check`
→ `VERDICT: BLOCKED (pre-render hard gates)`, `scene 6 corrupt/mixed-script`, exit 1,
no TTS/MP3/MP4 produced. `build_tts_script` on the same plan reports **8 FAIL blockers**
covering every panel claim (see table above).

### v011 gate drill (verified: free 7B + strict gates, 57 tests green)
The 12-header Mod-03 doc was grouped into an 8-scene concept frame (§14). The 7B's
narration then degenerated into per-scene title-echo stubs (`Is 02 me baselines plus
compare.`) with empty DDs — and the **pre-audio gate blocked them exactly as designed**
(9 FAILs: `tts_sentence_fragment` + `tts_required_concept_missing`), writing
`output/mod03_gates_v011.rejected.tts_script.json`, exit 1, no edge-tts. This proved
three things and drove three refinements:
- **A2.14a title-head sentence is intentional** (title is spoken once) — `_is_slide_fragment`
  now exempts the first sentence when it equals `spoken_title`, killing the false positive
  that LITERALLY every scene was producing (`The metric registry.` → "fragment").
- **A2.14b `verify` = deterministic thin-repair** — new `_repair_thin_narrations` rebuilds
  stubbed narration from the scene's own bullets+DD, driven by the SAME audio-gate FAIL codes
  (`fail_codes ⊆ {tts_required_concept_missing, tts_sentence_fragment,
  tts_too_short_critical, tts_unknown_token}`), so the repairer and gate share one source of
  truth. Rebuilds no longer echo the title (the spoken head is prepended once), use a
  12-token floor (bullet-only scenes like `Kind / Direction / Tolerance` otherwise could not
  qualify), and pass the 0-based index for skip-logic. Voice lead words (`Iska kaaran:`,
  `Design choice:`) are trusted by the unknown-token gate so Hinglish leads never false-fail.
- **A2.14c blocked builds now persist the plan** — the expensive LLM plan is saved to
  `*.plan.json` even when the pre-audio gate refuses TTS, so repair is `verify` → `render`,
  never "pay for a full rebuild".
- **A2.14d digit pronunciation** — single `0–9` digit rules (word-boundary) added to both
  voice tables so `Exit 3.` speaks `Exit three.` (`D36`, `M1`, `1/(n+1)` unaffected by
  longest-first matching). Kills the last `tts_symbol_heavy` on scene 7 of the real plan.
- **A2.14e teaching-claim bullet escape** — `tts_required_concept_missing` (and
  `_has_teaching_claim`) now pass when the spoken track carries ≥2 DISTINCT scene bullets
  verbatim (number-words normalized), mirroring the A2.13 "2+ teaching points" contract for
  title-shaped topics whose terms legitimately live in the slide title.
- **A2.14f digit-title head exemption** — the spoken-title head is matched by normalized
  word tokens (digit-words → digits, leading zeros stripped), so stub-style titles the 7B
  emits (`Is 02 me baselines plus compare`) are recognized as the deliberate first spoken
  sentence even when the title carries a number or inner punctuation (prefix match ≤8 words).
- **A2.14g file-token expansion** — `.py` joins `.json`/`.yml` in both voice pronunciation
  tables so a scene bullet like `Exit code generated by compare.py` speaks `compare dot py`
  instead of tripping `tts_symbol_heavy`.

Real-plan drill (second build, 5 FAILs, `output/mod03_gates_v011_002.plan.json`): the FULL
`verify` chain now deterministically cleans the free-7B plan to a green pre-audio gate
(`pre-audio gate : PASS`, 61 tests green) — the stub class produced by the free 8192-token
model is now repaired for free and the honest "needs a rebuild" block is reserved for scenes
that are GENUINELY content-starved.

## 14. Gemini "systemic pipeline / Docs-as-Code / video enrichment" reviews — adoption record

Three follow-up FOSS blueprint reviews (content-chunking + TTS sanitizer + budget
engine; enriched video compositing; Docs-as-Code CI/CD). Adopted what is
low-risk, deterministic, and dependency-free for this offline / free-7B studio;
deferred the GPU/ASR/avatar tiers with concrete open-source candidates.

### Adopted (implemented, 61 tests green)
| Suggestion | Implementation |
|---|---|
| 1:1 concept partitioning (no 3/12 truncation) | `plan._concept_headers` — stdlib `##`/`###` extraction (AST-lite; reviewer's `mistune`/`marko` not needed), bounded `AUTO_TRIM_MAX_SCENES=12`; with `concepts=…` the frame is `scene_target = len(concepts)` up to 12 (1:1, supersedes the earlier 8-scene merge — see §3.5.4); only docs with >12 concepts merge (`_concept_groups` labels EVERY concept, none dropped, loud overflow audit); `STUDIO_PROMPT` takes `{scene_count}` |
| Reject `len(scenes) != len(source_concepts)` | `_scene_count_problem` honors `plan["scene_target"]` (1:1 contract message); `_trim_scene_overflow` trims to the concept target; `_overflow_audit` records it; failed RAM only on LLM (retry path re-attaches contract keys) |
| Narrow the word-budget grip | plan grip cut `~70-110 words/scene` → **30-55** (matches the 40-50-for-20s rule; bans the Scene-2 114-word overflow class at generation time) |
| Text sanitization / Unicode gate | satisfied by §13 (`tts_text_corrupted`, Latin-only prompt, Devanagari+Cyrillic blockers) — no code change needed here |
| Monolingual voice tag | satisfied by §13/A2.12 (one concrete `tts_voice` + `provider:"edge-tts"` per plan) |
| Docs-as-Code single source of truth | `Makefile` (`make plan`→generate, `make verify`, `make build`, `make bakeoff`) + `.github/workflows/render.yml` (path/`workflow_dispatch` trigger → `uv sync` → `doc-to-studio build` → artifact upload; edge-tts stays free) |

### Deferred with open-source candidates (priority + acceptance criteria)
| Suggestion | Priority | Candidate | Why deferred |
|---|---|---|---|
| Forced alignment / word timecodes | P3 | `whisperx` (BSD-4) or `stable-ts`/`faster-whisper`, wav2vec2 align model | ASR model download + heavy CPU/GPU; current per-scene WAV sync is deterministic. Acceptance: produce `timecodes.json`; VTT subtitles |
| Kinetic reveals / pan-zoom / focus dim | P3 | ffmpeg `zoompan` + `drawbox` (native, free); `moviepy` already a declared dep but unused in `src` | new motion path risks sync regressions; needs a real render test. Import `moviepy` only here, not the prompt path |
| BGM + auto-ducking + SFX cues | P3 | ffmpeg `sidechaincompress` + `amix` (free) | new audio-mix path; keep loudnorm master. `--bgm` optional flag |
| AI avatar PiP (SadTalker/LivePortrait) | P4 | LivePortrait (MIT), SadTalker (Apache-2.0/Academic) — GPU required | heavy GPU/VRAM, out of the free-7B scope; HeyGen excluded (paid) |
| Marp/Reveal.js slide engine | P4 | Marp CLI (MIT) | existing python-pptx dark-theme builder satisfies B-rules; Marp is a parallel export, not a replacement |

### Reviewer example-script caveat
The sample `moviepy` loop (one `TextClip` per word) would spawn thousands of clips
and die on CPU — if enrichment lands it must batch reveals per scene, not per word
(ffmpeg `drawbox`/`zoompan` keyframes, or grouped overlay clips).

## 15. Gemini pipeline-output review (`v011_017`) — adoption record

Reviewed `output/mod03_gates_v011_017.plan.json`, the persisted TTS script, and
the sample plans against the shipped code. Reconciliation below; anything marked
ADOPTED is implemented and must not be re-suggested.

### Gap 1 — spoken `design_decision` described as prompt leakage
| Item | Verdict | Resolution |
|---|---|---|
| Scene 2 narration contains `Iska ahem reason: Registry defines types ...` | PARTIAL ADOPT | Speaking `design_decision` is the ADOPTED interview-framing contract (§3.4 step 6, §11) — it is not prompt leakage and must NOT be stripped. The real defect was the colon-terminated `_DD_LEADS`, which `repair_speech_punctuation` flattened into `Iska ahem reason Registry ...`. Leads are now grammatical and colon-free (`config._DD_LEADS`, e.g. `Iska ahem reason yeh hai ki `). |
| Add "narration must never contain system justification" to the prompt | REJECTED | would contradict the explicit "lean on design_decision" instruction and delete the interview-ready why-this content the product requires |

### Gap 2 — diagrams and renderable structure
| Item | Verdict | Resolution |
|---|---|---|
| Replace `visual_diagram` text with Graphviz/Mermaid/JSON nodes+edges | NOT ADOPTED | already rejected in §14: `visual_diagram` stays a text chain parsed by `slides._parse_diagram`; JSON node/edge objects would add a free-7B failure surface for no renderer gain |
| Plain `--->` arrows do not render as rich cards | ALREADY IMPLEMENTED | `slides.render_slide` + `pptx._ppt_diagram` draw node boxes with GOLD chevron arrows; the raw arrow string never reaches the deck |
| Empty diagram fallback for registry / verdict-kind scenes | ADOPTED | `_ensure_technical_visuals` now derives deterministic diagrams from `source_chunk`: `[Gate: hard FAIL] ---> [Guardrail: soft REVIEW] ---> [Info: recorded only]` and `[Metric] ---> [Kind + direction] ---> [Tolerance + unit]`. Verified: all 9 scenes in the repaired v011_017 plan carry a diagram |

### Gap 3 — pagination/content mismatch (real defect)
| Item | Verdict | Resolution |
|---|---|---|
| `bullets` and `bullet_pages` diverged after deterministic bullet pruning | ADOPTED | `_drop_ungrounded_slide_text` and `verify` now re-run `_paginate_plan_slides` after every bullet mutation; `validate._scene_metadata_problems` HARD-FAILS any scene where `flatten(bullet_pages) != bullets` or a page exceeds 4 items |
| Gemini's proposed `len(flatten(bullet_pages))` check | ADOPTED | shipped as a hard render/verify invariant with deterministic repair, not a post-hoc report |

### Gap 4 — truncation, schema, and synchronization
| Item | Verdict | Resolution |
|---|---|---|
| `sample2` plan is truncated mid-JSON | REJECTED (not reproducible) | both `mod03_gates_v011_017.sample1.plan.json` and `.sample2.plan.json` parse as valid JSON; the reported `Absolute/` string is a complete bullet, not a truncation. Existing `_was_truncated` + `choose_planner_budget` ladder already shrinks the window and retries |
| Add `json-repair` / raise `max_tokens` | NOT ADOPTED | the endpoint shares an 8192-token window; the deterministic retry ladder is the in-scope lever and adding a repair library would mask, not fix, an over-budget plan |
| Adopt Pydantic `ScenePlan` / `VideoPlan` | ALREADY ADOPTED | `studio/schema.py` (`SlideScene`, `LessonPlan`, `TTSClip`) is wired into `plan_lesson`; it also normalizes `bullets`/`steps`/`flow` strings before list validation |
| Word-level timestamps from `edge-tts` for subtitles/bullet highlights | DEFERRED (P3) | identical to the §14 deferred forced-alignment item; current slide duration is `max(audio.duration, 0.1)` and stays deterministic without an ASR model |

### Status reconciliation
- §9 A2.5 — PASS, note extended with the colon-free `_DD_LEADS` fix.
- §9 B1.2 — PARTIAL → **PASS** (schema max 4 bullets + hard `bullet_pages` invariant).
- §9 B3.5 — remains PARTIAL (deterministic fallbacks + node-box rendering shipped; human content review still required).
- Evidence: `verify output/mod03_gates_v011_017.plan.json` → `VERDICT: PASS`, pre-audio gate PASS, all 9 scenes pagination-consistent with a diagram; Ruff, mypy, and pytest green.