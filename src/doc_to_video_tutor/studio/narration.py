"""Narration generation repair/safety helpers."""

from __future__ import annotations

import json
import re

from .config import _NARR_MIN_TOKENS, NARRATION_PROMPT
from .llm import _ask_llm_stable, _bounded_content, planner_output_budget
from .speech import audit_tts_script, build_tts_script
from .text import (
    _entity_tokens,
    _nar_3grams_t,
    _nar_3grams_t_ordered,
    _nar_gram_key,
    _nar_tokens,
)
from .voice import _MHE_VOICE, NarrationVoice, _opener_module_name

_REPAIRABLE_TTS_FAILS = frozenset({
    "tts_required_concept_missing", "tts_sentence_fragment",
    "tts_too_short_critical", "tts_unknown_token",
})


def narration_matches_voice_policy(plan: dict,
                                   voice: NarrationVoice | None = None
                                   ) -> tuple[bool, str]:
    """Check narration against the voice's language policy.

    Returns (passed, reason). Mismatch is advisory (soft gate); it never breaks
    a build because the free 7B legitimately alternates pure-English technical
    sentences with mixed-register ones.
    """
    voice = voice or _MHE_VOICE
    pol = voice.policy
    if not pol.must_not_be_pure_english:
        return True, f"voice {voice.name}: pure English legal"
    texts = [plan.get("opening", "")]
    texts += [sc.get("narration", "") for sc in plan.get("scenes", [])]
    texts = [t for t in texts if isinstance(t, str)]
    tokens = [w for t in texts for w in _nar_tokens(t)]
    if not tokens:
        return True, "no narration to check"
    hits = len(frozenset(tokens) & pol.required_markers)
    if hits < pol.min_marker_hits:
        return False, (f"narration looks pure English ({hits} marker hit(s), "
                       f"< {pol.min_marker_hits} required)")
    return True, f"voice {voice.name}: {hits} marker hit(s)"
def _narration_is_pure_english(plan: dict,
                               voice: NarrationVoice | None = None) -> bool:
    """Advisory: narration looks pure English for this voice's policy."""
    passed, _reason = narration_matches_voice_policy(plan, voice)
    return not passed
def _protected_terms(plan: dict) -> frozenset[str]:
    """Grams that are legit technical terminology, exempt from auto-deletion.

    Built deterministically from the plan itself (takeaways, topics, title,
    design decisions, scene titles) PLUS the build-time source trigrams saved
    into the plan (``protected_trigrams``). Domain phrases like
    'retrieval augmented generation' survive the repeat enforcer so later
    scenes keep their meaning; the audit still counts and reports them.
    """
    texts: list[str] = [str(t) for t in (plan.get("takeaways") or [])]
    for sc in plan.get("scenes", []):
        texts.append(str(sc.get("title", "")))
        texts.append(str(sc.get("design_decision", "")))
        for t in (sc.get("takeaways") or []):
            texts.append(str(t))
    protected = set()
    for text in texts:
        protected |= set(_nar_3grams_t(_nar_tokens(text)))
    entities = _entity_tokens(*texts, *[str(t) for t in (plan.get("topics") or [])],
                              str(plan.get("title", "")))
    for saved in (plan.get("protected_trigrams") or []):
        saved_g = " ".join(_nar_tokens(str(saved)))
        if saved_g:
            protected.add(saved_g)
    protected_grams: set[str] = set(protected)
    # A gram containing an entity token is also protected (e.g. 'rag ke against').
    for sc in plan.get("scenes", []):
        toks = _nar_tokens(str(sc.get("narration", "")))
        for j in range(max(0, len(toks) - 2)):
            gram = tuple(toks[j:j + 3])
            if len(gram) == 3 and any(w in entities for w in gram):
                protected_grams.add(_nar_gram_key(gram))
    for saved_phrase in (plan.get("protected_entities") or []):
        entities = entities | frozenset({str(saved_phrase).lower()})
    return frozenset(protected_grams)
def _persist_protected_trigrams(plan: dict, content: str) -> list[str]:
    """Source-derived terminology grams, saved into the plan for later stages.

    A 3-token window from a source line that touches an entity token (acronym,
    CamelCase, numeric, or path-like identifier) is protected terminology: the
    repeat enforcer must never delete it and the gate never fails on it. Saved
    into the plan so verify/review/render reproduce the same policy without the
    source text.
    """
    entities = _entity_tokens(content, str(plan.get("title", "")))
    grams: set[str] = set()
    for line in content.splitlines():
        toks = _nar_tokens(line)
        for j in range(max(0, len(toks) - 2)):
            gram = tuple(toks[j:j + 3])
            if len(gram) == 3 and any(w in entities for w in gram):
                grams.add(_nar_gram_key(gram))
    out = sorted(grams)
    plan["protected_trigrams"] = out[:4096]
    return out
def _unsafe_repeat_scenes(plan: dict,
                          protected: frozenset[str] | None = None) -> list[int]:
    """Scenes whose narration has a BANNED repeat that cannot be repaired.

    Two shapes are unrepairable without gutting real content:
    (a) the narration is too short to lose three tokens (below the minimal
        spoken length) and still carries a banned phrase; or
    (b) the narration is a full duplicate — every 3-word window is a banned,
        already-spoken phrase — so repairing it would erase the scene entirely.

    Both force a plan resample (hard gate); repaired scenes never ship damaged.
    """
    banned_set = frozenset(_narration_repeat_report(plan, protected)[0])
    if not banned_set:
        return []
    unsafe: list[int] = []
    seen: set[str] = set()
    for i, sc in enumerate(plan.get("scenes", []), 1):
        toks = _nar_tokens(str(sc.get("narration", "")))
        grams = _nar_3grams_t(toks)
        banned_here = grams & banned_set
        # Unrepairable only for REPEATS of earlier content: full duplicate
        # (every gram already spoken) or too short to trim three tokens.
        if banned_here and banned_here <= seen and (
                grams <= seen or len(toks) <= _NARR_MIN_TOKENS):
            unsafe.append(i)
        seen |= banned_here
    return unsafe
def _narration_repeat_report(plan: dict,
                             protected: frozenset[str] | None = None
                             ) -> tuple[list[str], list[str]]:
    """Split the gate's repeats into (banned, protected-terminology).

    Banned = repeated >=3-word phrases NOT protected -> the gate's contract.
    Protected = repeated phrases on the allowlist (terminology) -> reported
    separately, never a failure.
    """
    protected = frozenset() if protected is None else protected
    seen: dict[str, int] = {}
    for sc in plan.get("scenes", []):
        for g in _nar_3grams_t_ordered(_nar_tokens(str(sc.get("narration", "")))):
            seen[g] = seen.get(g, 0) + 1
    banned = sorted(g for g, c in seen.items() if c >= 2 and g not in protected)
    prot = sorted(g for g, c in seen.items() if c >= 2 and g in protected)
    return banned, prot
def _narration_tail_repeat(plan: dict, quiet: bool = False,
                           protected: frozenset[str] | None = None) -> list[str]:
    """Banned repeated >=3-word narration phrases (protected repeats excluded)."""
    banned, _prot = _narration_repeat_report(plan, protected)
    if banned and not quiet:
        print(f"\n  [1/5] repeating narration phrase detected: {banned[:3]} ...",
              end="", flush=True)
    return banned
def _clean_narration(nar: str) -> str:
    """Remove punctuation artifacts left when template sentences are dropped."""
    nar = re.sub(r"\.\s*\.", ".", nar)
    nar = re.sub(r"\s*'\s*\.", ".", nar)
    nar = re.sub(r"\.\s*'\s*", ". ", nar)
    nar = re.sub(r"\s*\.\s*$", ".", nar)
    nar = re.sub(r"(?<!\s):(?=\S)", ": ", nar)
    nar = re.sub(r"(?<=[.!?])\s+([a-z])",
                 lambda match: " " + match.group(1).upper(), nar)
    return re.sub(r"\s+", " ", nar).strip()
def _drop_repeated_filler(plan: dict, quiet: bool = False) -> int:
    """Drop within-narration sentences that repeat earlier ones (ANY language).

    Language-agnostic token pass: if an already-spoken 3-gram (from an earlier
    WORD token window), the model's phrase-level repetition is reduced. The
    cross-scene variant lives in _drop_shared_narration_sentences (called from
    _dedupe_narration_templates). No script/Hinglish patterns are hardcoded here
    because the same token rule works for any narration language.
    """
    changed = 0
    for sc in plan.get("scenes", []):
        nar = str(sc.get("narration", ""))
        if not nar:
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", nar) if s.strip()]
        kept: list[str] = []
        seen_grams: set[str] = set()
        for s in sentences:
            grams = set(_narration_3grams(s))
            if grams & seen_grams:
                changed += 1
                continue
            seen_grams |= grams
            kept.append(s)
        sc["narration"] = _clean_narration(" ".join(kept))
    if changed and not quiet:
        print(f"\n  [1/5] dropped {changed} repeated narration filler "
              f"sentence(s).", end="", flush=True)
    return changed
def _drop_within_scene_exact_repeats(plan: dict, quiet: bool = False) -> int:
    changed = 0
    for sc in plan.get("scenes", []):
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+",
                                                str(sc.get("narration", "")).strip())
                     if s.strip()]
        seen: set[tuple[str, ...]] = set()
        kept: list[str] = []
        for sentence in sentences:
            key = tuple(_nar_tokens(sentence))
            if key and key in seen:
                changed += 1
                continue
            if key:
                seen.add(key)
            kept.append(sentence)
        if len(kept) != len(sentences):
            sc["narration"] = _clean_narration(" ".join(kept))
    if changed and not quiet:
        print(f"\n  [1/5] dropped {changed} exact repeated narration "
              f"sentence(s) within a scene.", end="", flush=True)
    return changed

def _enforce_unique_narration_trigrams(plan: dict, quiet: bool = False,
                                       protected: frozenset[str] | None = None
                                       ) -> int:
    """Make the gate pass in ANY language without mutilating narration.

    Repair hierarchy, SENTENCE-AWARE (see LLD / Perplexity adoption):
    1) protected terminology / entity phrases are NEVER removed (the gate
       reports them separately, never a failure);
    2) a repeated filler SENTENCE (every word belongs to a banned >=3-word
       phrase) is dropped WHOLE — removing it carries away nothing unique;
    3) in a sentence that also holds real content, only the banned phrase is
       trimmed; if that would drag the surviving sentence below the minimal
       spoken length the scene is marked 'unsafe' and NOT touched, so the
       build resamples instead of shipping damaged narration;
    4) the scan repeats until no safely-repairable banned phrase remains.

    Exactly matches the review gate's contract (canonical tokens, within-scene
    and cross-case repeats included). Language-agnostic for whitespace-delimited
    text: it only compares token windows, never hardcoded words or scripts.
    """
    scenes = plan.get("scenes", [])
    if len(scenes) < 1:
        return 0
    protected = frozenset() if protected is None else protected
    unsafe_scenes: list[int] = plan.setdefault("_narration_unsafe_scenes", [])
    removed = _drop_within_scene_exact_repeats(plan, quiet=quiet)

    def _sentences(nar: str) -> list[tuple[str, list[str]]]:
        raw = re.split(r"(?<=[.!?])\s+", nar.strip())
        return [(s, toks) for s in raw if (toks := _nar_tokens(s))]

    sentence_spans = [
        _sentences(str(sc.get("narration", "")))
        for sc in scenes
    ]

    def _scan() -> tuple[set[str], dict[str, tuple[int, int, int]]]:
        counts: dict[str, int] = {}
        first: dict[str, tuple[int, int, int]] = {}
        for i, sentences in enumerate(sentence_spans):
            for sentence_index, (_sentence, tokens) in enumerate(sentences):
                for j in range(max(0, len(tokens) - 2)):
                    gram = _nar_3grams_t_ordered(tokens[j:j + 3])
                    if not gram:
                        continue
                    key = gram[0]
                    counts[key] = counts.get(key, 0) + 1
                    first.setdefault(key, (i, sentence_index, j))
        banned = {g for g, count in counts.items()
                  if count >= 2 and g not in protected}
        return banned, first

    banned, first = _scan()
    if not banned:
        unsafe_scenes.clear()
        plan["_narration_unsafe_scenes"] = unsafe_scenes
        return removed
    for _ in range(4):
        if not banned:
            break
        removed_iter = 0
        for i, sc in enumerate(scenes):
            nar_before = str(sc.get("narration", ""))
            kept_sents: list[str] = []
            for sentence_index, (sentence, tokens) in enumerate(sentence_spans[i]):
                grams = _nar_3grams_t_ordered(tokens)
                banned_in_s = {g for g in grams if g in banned}
                if not banned_in_s:
                    kept_sents.append(sentence)
                    continue
                first_at_sentence = all(
                    first[g][0] == i and first[g][1] == sentence_index
                    for g in banned_in_s
                )
                internal_repeat = len(grams) != len(set(grams))
                if grams and set(grams) <= banned and not internal_repeat:
                    if first_at_sentence:
                        kept_sents.append(sentence)
                    else:
                        removed += len(tokens)
                    continue
                keep_tokens: list[str] = []
                j = 0
                while j < len(tokens):
                    grams_at = _nar_3grams_t_ordered(tokens[j:j + 3])
                    gram = grams_at[0] if grams_at else None
                    if (gram is not None and gram in banned
                            and first[gram] != (i, sentence_index, j)):
                        j += 3
                        continue
                    keep_tokens.append(tokens[j])
                    j += 1
                cut = len(tokens) - len(keep_tokens)
                if not cut:
                    kept_sents.append(sentence)
                    continue
                if len(keep_tokens) < _NARR_MIN_TOKENS:
                    if i + 1 not in unsafe_scenes:
                        unsafe_scenes.append(i + 1)
                    kept_sents.append(sentence)
                    continue
                removed += cut
                if keep_tokens:
                    kept_sents.append(" ".join(keep_tokens))
            new_nar = _clean_narration(" ".join(kept_sents))
            if new_nar != nar_before and new_nar.strip():
                sc["narration"] = new_nar
                sentence_spans[i] = _sentences(new_nar)
                removed_iter += 1
            elif new_nar != nar_before and not new_nar.strip():
                if i + 1 not in unsafe_scenes:
                    unsafe_scenes.append(i + 1)
        if removed_iter == 0:
            break
        banned, first = _scan()
    if not banned:
        unsafe_scenes.clear()
    weak = sorted(banned)
    unsafe_scenes[:] = sorted(set(unsafe_scenes))
    plan["_narration_unsafe_scenes"] = unsafe_scenes
    if removed and not quiet:
        print(f"\n  [1/5] dropped {removed} repeated narration "
              f"trigram(s) (generic guarantee).", end="", flush=True)
    if weak and not quiet:
        print(f"\n  [1/5] WARNING: {len(weak)} repeated phrase(s) survive in "
              f"scenes too short to repair safely: {weak[:3]} ...", end="", flush=True)
    return removed
def _repair_unsafe_narrations(plan: dict,
                              voice: NarrationVoice | None = None,
                              protected: frozenset[str] | None = None,
                              quiet: bool = False) -> int:
    """Deterministic narration-rebuild fallback for scenes the enforcer gave up on.

    Called AFTER ``_enforce_unique_narration_trigrams``. For every scene that
    still carries a banned repeat (too short to trim safely, or a full
    duplicate), rebuild its spoken track deterministically from the scene's own
    validated fields (title + design_decision + a distinct bullet), using only
    the voice's words. Anything the rebuild could not make repeat-clean is left
    untouched so the hard pre-render gate still reports it; repaired scenes are
    re-verified against the whole-plan repeat report so the gate stays honest.
    """
    voice = voice or _MHE_VOICE
    protected = frozenset() if protected is None else protected
    scenes = plan.get("scenes", [])
    if not scenes:
        return 0
    banned, _ = _narration_repeat_report(plan, protected)
    if not banned:
        return 0
    banned_set = set(banned)
    repaired = 0
    for idx, sc in enumerate(scenes):
        grams = _narration_3grams(str(sc.get("narration", "")))
        if not (grams & banned_set):
            continue
        rebuilt = _rebuild_scene_narration(sc, idx, scenes, voice)
        if not rebuilt or rebuilt == str(sc.get("narration", "")):
            continue
        sc["narration"] = rebuilt
        repaired += 1
    if repaired:
        # Re-run the sentence-aware enforcer over the rebuilt narration so any
        # harmless overlap with a PROTECTED term is kept and real repeats drop.
        _enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    if repaired and not quiet:
        print(f"\n  [1/5] deterministically rebuilt narration for {repaired} "
              f"unsafe scene(s).", end="", flush=True)
    return repaired
def _repair_thin_narrations(plan: dict, voice: NarrationVoice | None = None,
                            protected: frozenset[str] | None = None,
                            quiet: bool = False) -> int:
    """Deterministic rebuild for stubbed / transition-only narrations.

    The free 7B occasionally answers every scene with a one-line title echo
    ('Is 02 me baselines plus compare.') plus an empty design_decision. Those
    clips survive the repeat enforcer (no BANNED phrase in them) but die at the
    pre-audio gate (too-short + teaching-claim). A scene is rebuildable iff the
    AUDIO GATE itself (build_tts_script -> audit_tts_script) emits a FAIL in a
    repairable class - the repairer and the gate share ONE source of truth, so
    verify cannot claim a fixed scene the gate still rejects. Rebuilt narration
    comes from the scene's own fields exactly like ``_rebuild_scene_narration``;
    scenes the deterministic rebuild cannot make gate-clean are left untouched
    so the hard pre-audio gate still reports them honestly.
    """
    voice = voice or _MHE_VOICE
    protected = frozenset() if protected is None else protected
    scenes = plan.get("scenes", [])
    if not scenes:
        return 0
    clips = build_tts_script(plan, voice).get("clips", [])
    fails_idx: set[int] = set()
    for clip in clips:
        if clip.get("role") != "scene":
            continue
        findings = audit_tts_script([clip], voice, plan)
        fail_codes = {f.code for f in findings if f.severity == "FAIL"}
        if fail_codes and fail_codes <= _REPAIRABLE_TTS_FAILS:
            fails_idx.add(int(clip.get("index", 0)))
    if not fails_idx:
        return 0
    repaired = 0
    for idx in sorted(fails_idx):
        sc = scenes[idx - 1]
        nar = str(sc.get("narration", ""))
        # A rebuilt narration must survive the pre-audio gate (~20 spoken
        # words/scene incl. the spoken title head). The 12-token floor is the
        # legacy build floor; when real source prose is available (Phase-3
        # source_chunk) target the ~18-token floor so the hydration is long
        # enough to actually pass the TTS audit instead of minting a 14-word
        # narration that fails it.
        floor = _NARR_MIN_TOKENS
        if str(sc.get("source_chunk", "")).strip():
            floor = _NARR_MIN_TOKENS + 6
        rebuilt = _rebuild_scene_narration(sc, idx - 1, scenes, voice,
                                           min_tokens=floor)
        if not rebuilt or rebuilt == nar:
            continue
        sc["narration"] = rebuilt
        repaired += 1
    if repaired:
        _enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    if repaired and not quiet:
        print(f"\n  [1/5] deterministically rebuilt narration for {repaired} "
              f"thin scene(s).", end="", flush=True)
    return repaired


def _thin_narration(nar: str, sc: dict, voice: NarrationVoice) -> bool:
    """Narration is thin if it could never pass the pre-audio gate: too few
    spoken words, or too few real teaching words after openers/closers and
    the title echo are stripped (mirrors ``_has_teaching_claim``)."""
    if len(_nar_tokens(nar)) < _NARR_MIN_TOKENS + 6:
        return True
    title_words = set(re.sub(r"[\u2192:()\d&]", " ",
                             str(sc.get("title", ""))).casefold().split())
    text = str(nar).casefold()
    for phrase in list(voice.openers) + list(voice.closers) + list(voice.narr_heads):
        text = text.replace(str(phrase).casefold(), " ")
    content = [w for w in text.split()
               if len(w) >= 3 and w.strip(".,;:!?").casefold() not in title_words]
    return len(content) < 8


def _rebuild_scene_narration(sc: dict, idx: int, scenes: list[dict],
                             voice: NarrationVoice,
                             min_tokens: int = _NARR_MIN_TOKENS) -> str:
    """Deterministic narration for one scene from its validated fields.

    Rebuild the spoken track as: opener + design_decision lead + distinct
    bullets. The title is NOT re-spoken here - ``build_tts_script`` prepends the
    spoken title head itself, so echoing it in the narration would trip the
    fragment/repeat gates. Only the voice's words and the scene's OWN content
    are used (no hardcoded Hinglish grammar anywhere in Layer A), and any piece
    whose 3-grams collide with another scene's narration is skipped, so the
    rebuild cannot invent a new banned repeat. Returns "" if nothing usable.
    """
    other_grams: set[str] = set()
    for j, o in enumerate(scenes):
        if j == idx:
            continue
        other_grams |= _narration_3grams(str(o.get("narration", "")))
    for t in (sc.get("takeaways") or []):
        other_grams |= _narration_3grams(str(t))

    module = _opener_module_name(sc)
    opener = _pick_opener(voice, idx, module, other_grams)

    parts: list[str] = [opener] if opener else []
    dd = str(sc.get("design_decision", "")).strip().strip(".!? ")
    if dd and not (_narration_3grams(dd) & other_grams):
        lead = voice.dd_leads[idx % len(voice.dd_leads)].strip()
        parts.append(f"{lead}{dd}.")
    for b in (sc.get("bullets") or [])[:5]:
        bt = str(b).strip().strip(".!? ")
        if not bt or (_narration_3grams(bt) & other_grams):
            continue
        parts.append(f"{bt}.")
    nar = _clean_narration(" ".join(p.strip() for p in parts if p.strip()))
    if len(_nar_tokens(nar)) < min_tokens:
        # Phase-3 hydration: when a scene's own fields can't reach the floor,
        # pull real teaching sentences straight from the source docs (the
        # scene's annotated source_chunk). Each sentence is collision-checked
        # against every other scene's narration so the rebuild cannot invent a
        # new banned repeat. Real doc prose is denser and more unique than the
        # structural template, so this resolves the truly content-starved
        # scene class instead of leaving it for a whole-plan LLM resample.
        for s in _source_sentences(str(sc.get("source_chunk", ""))):
            if _narration_3grams(s) & other_grams:
                continue
            parts.append(s)
            nar = _clean_narration(" ".join(p.strip() for p in parts if p.strip()))
            if len(_nar_tokens(nar)) >= min_tokens:
                break
    if len(_nar_tokens(nar)) >= min_tokens:
        return nar
    return ""
def _pick_opener(voice: NarrationVoice, start_idx: int, module: str,
                 other_grams: set[str]) -> str:
    """A collision-free voice opener for the scene being rebuilt.

    Opener templates rotate by scene index; when the pool is smaller than the
    lesson, two scenes can legally share the SAME opener string and trip the
    trigram repeat gate. Cycle from `start_idx` and return the first opener
    whose 3-grams are clear of every other scene's live narration (the active
    n-gram registry), falling back to the plain rotation when the whole pool
    collides.
    """
    pool = voice.openers or [""]
    for k in range(len(pool)):
        cand = pool[(start_idx + k) % len(pool)].format(m=module).strip(" -")
        if not cand or not (_narration_3grams(cand) & other_grams):
            return cand
    return pool[start_idx % len(pool)].format(m=module).strip(" -")
def _source_sentences(text: str) -> list[str]:
    """Complete, teachable prose sentences (>=8 words) from a source excerpt."""
    out: list[str] = []
    for s in re.split(r"(?<=[.!?])\s+", text.strip()):
        s = s.strip()
        if len(_nar_tokens(s)) >= 8:
            out.append(s)
    return out
def _dedupe_narration_templates(plan: dict, quiet: bool = False,
                                voice: NarrationVoice | None = None,
                                protected: frozenset[str] | None = None) -> int:
    """Collapse the 7B's canned narration skeletons into unique, on-topic lines.

    The free model reuses the same fill-in sentences across scenes ('ek ahem
    module hai, hum yeh samajhenge ki kya ...', 'chaliye seekhte hai kya ...'),
    tripping the repeated-phrase gate with the SAME trigram in many narrations.
    Every duplicate skeleton sentence is dropped; the real 'kya ... hain,
    kaise ... hain' content it carried is re-attached once under a scene-unique
    and title-agnostic head chosen in rotation, so no >=3-word phrase repeats.
    """
    voice = voice or _MHE_VOICE
    changed = 0
    changed += _drop_repeated_filler(plan, quiet=True)
    if not voice.skeletons:
        return changed
    head_slot = 0
    for sc in plan.get("scenes", []):
        nar = str(sc.get("narration", ""))
        matched = False
        for sk in voice.skeletons:
            if sk.search(nar):
                matched = True
                break
        if not matched:
            continue
        content = ""
        for sk in voice.skeletons:
            m = sk.search(nar)
            if m and (m.group("c") or "").strip():
                content = m.group("c").strip()
                break
        nar2 = nar
        for sk in voice.skeletons:
            nar2 = sk.sub(" ", nar2)
        nar2 = re.sub(r"\s+", " ", nar2).strip()
        if content:
            probe = " ".join(content.split()[:3])
            if "kya " + probe not in nar2:
                head = voice.narr_heads[head_slot % len(voice.narr_heads)]
                head_slot += 1
                nar2 = f"{head} kya {content}. ".capitalize() + nar2
        nar2 = _clean_narration(nar2)
        if nar2 != nar:
            sc["narration"] = nar2
            changed += 1
    changed += _drop_shared_narration_sentences(plan)
    changed += _enforce_unique_narration_trigrams(plan, quiet=True,
                                                  protected=protected)
    if not quiet and changed:
        print(f"\n  [1/5] de-duplicated narration templates across "
              f"{changed} scene(s).", end="", flush=True)
    return changed
def _drop_shared_narration_sentences(plan: dict, quiet: bool = False) -> int:
    """Drop cross-scene repeated narration SENTENCES (canned 7B follow-up).

    'aaj aapne dekha ki ...' is an attractor sentence the free model pastes
    after its opener in EVERY scene. The repeat gate is trigram-based, so even
    the second-and-third sentences of that block trip it. Any sentence whose
    leading 6 tokens are shared by >=2 scenes is collapsed into a single
    occurrence (kept once, dropped everywhere else).
    """
    scenes = plan.get("scenes", [])
    if len(scenes) < 2:
        return 0
    seen: dict[str, list[int]] = {}
    stored: list[list[str]] = []
    for i, sc in enumerate(scenes):
        sentences = re.split(r"(?<=[.!?])\s+", str(sc.get("narration", "")).strip())
        stored.append(sentences)
        for s in sentences:
            toks = _nar_tokens(s)
            if len(toks) >= 3:
                seen.setdefault(" ".join(toks[:6]), []).append(i)
    shared = {k for k, idxs in seen.items() if len(set(idxs)) >= 2}
    if not shared:
        return 0
    kept_at: dict[str, int] = {}
    dropped = 0
    for i, sc in enumerate(scenes):
        keep: list[str] = []
        for s in stored[i]:
            toks = _nar_tokens(s)
            key = " ".join(toks[:6]) if len(toks) >= 3 else ""
            if key in shared and key in kept_at:
                dropped += 1
                continue
            if key in shared:
                kept_at[key] = i
            keep.append(s)
        sc["narration"] = _clean_narration(" ".join(keep))
    if dropped and not quiet:
        print(f"\n  [1/5] dropped {dropped} cross-scene repeated narration "
              f"sentence(s).", end="", flush=True)
    return dropped
def _narration_3grams(nar: str) -> set[str]:
    """Canonical 3-grams of a narration (same rule as the repeat gate)."""
    return set(_nar_3grams_t(_nar_tokens(nar)))
def _deepen_narrations(plan: dict, quiet: bool = False,
                       voice: NarrationVoice | None = None) -> int:
    """Append the scene's design_decision (and the final takeaways) on screen.

    The 7B narrations are short (~35 words), leaving the lesson far under its
    duration target. Speaking each scene's WHY-THIS reasoning aloud deepens the
    'interview-ready' feel and extends runtime deterministically. Each lead-in
    is unique so no repeated-phrase trigram is introduced; a design_decision
    whose reason clause shares a trigram with a takeaway line is trimmed to its
    'X not Y' head to keep the phrase gate clean.
    """
    voice = voice or _MHE_VOICE
    takeaway_lines = [str(t) for t in (plan.get("takeaways") or [])]

    def _dd_spoken(dd: str) -> str:
        if not dd:
            return ""
        grams = _narration_3grams(dd)
        if grams and any(grams & _narration_3grams(t) for t in takeaway_lines):
            return re.split(r"\s+because\s+", dd, maxsplit=1, flags=re.I)[0].strip(" .,")
        return dd.rstrip(" .")

    changed = 0
    all_scenes = plan.get("scenes", [])
    # Seed the decision ledger with any design_decision ALREADY carried in the
    # narration text (a build-time or earlier deepen pass). Without this seed,
    # a shared dd phrase (e.g. every scene's 'why step9 chose it: ...') is only
    # blocked within a single call, so a repeated verify pass re-assigns it to a
    # DIFFERENT scene each time and the fixer chain never converges. A sextuple
    # must be entirely contained in its owning narration (i.e. the whole
    # decision was spoken before), not merely share one coincidental trigram.
    spoken_dd: list[set[str]] = []
    for sc in all_scenes:
        g = _narration_3grams(str(sc.get("design_decision", "")).strip())
        if g and g <= _narration_3grams(str(sc.get("narration", ""))):
            spoken_dd.append(g)
    live_grams = set().union(*(_narration_3grams(str(s.get("narration", "")))
                               for s in all_scenes)) if all_scenes else set()
    other_grams = set(live_grams)
    for idx, sc in enumerate(plan.get("scenes", [])):
        nar = str(sc.get("narration", ""))
        tail = ""
        dd = str(sc.get("design_decision", "")).strip()
        dd_grams = _narration_3grams(dd)
        if dd_grams & _narration_3grams(nar):
            dd = ""
        if dd and any(dd_grams & prev for prev in spoken_dd):
            dd = ""
        if dd:
            tail += voice.dd_leads[idx % len(voice.dd_leads)] + _dd_spoken(dd).rstrip(".")
            spoken_dd.append(dd_grams)
        if (sc.get("takeaways") and any(str(t).strip() for t in sc["takeaways"])
                and voice.takeaways_marker not in nar):
            points = "; ".join(
                str(t).strip().rstrip(".") for t in sc["takeaways"]
                if not (_narration_3grams(str(t)) & other_grams))
            if points:
                tail += (voice.takeaways_any_lead if tail else
                         voice.takeaways_lead)
                tail += points + "."
        if tail:
            sc["narration"] = _clean_narration(
                (nar.rstrip() + " " + tail.strip()).strip())
            other_grams |= _narration_3grams(sc["narration"])
            changed += 1
    if changed and not quiet:
        print(f"\n  [1/5] spoke {changed} scene(s) deeper (decision + "
              f"takeaways).", end="", flush=True)
    return changed


def _trim_narration_word_count(plan: dict, voice: NarrationVoice | None = None,
                                max_words: int = 90, quiet: bool = False) -> int:
    voice = voice or _MHE_VOICE
    scenes = plan.get("scenes", [])
    trimmed = 0
    for index, sc in enumerate(scenes, 1):
        original = str(sc.get("narration", ""))
        current = original
        changed = False
        for _ in range(32):
            script = build_tts_script(plan, voice)
            clip = next((item for item in script.get("clips", [])
                         if item.get("role") == "scene"
                         and item.get("index") == index), None)
            if clip is None or int(clip.get("word_count", 0)) <= max_words:
                break
            sentences = re.split(r"(?<=[.!?])\s+", current.strip())
            if len(sentences) > 1:
                candidate = _clean_narration(" ".join(sentences[:-1]))
            else:
                words = current.split()
                candidate = _clean_narration(" ".join(words[:max_words]))
            if not candidate or candidate == current:
                break
            sc["narration"] = candidate
            current = candidate
            changed = True
        if changed:
            script = build_tts_script(plan, voice)
            clip = next((item for item in script.get("clips", [])
                         if item.get("role") == "scene"
                         and item.get("index") == index), None)
            findings = audit_tts_script([clip], voice, plan) if clip else []
            blocking = {"tts_too_long_critical", "tts_too_short_critical",
                        "tts_required_concept_missing", "tts_sentence_fragment"}
            if any(f.code in blocking for f in findings):
                sc["narration"] = original
            else:
                trimmed += 1
    if trimmed and not quiet:
        print(f"\n  [1/5] trimmed {trimmed} narration(s) to <={max_words} "
              f"spoken words.", end="", flush=True)
    return trimmed


def _narrations_from_json(raw: str, expected: int) -> list[dict] | None:
    """Parse + validate the narrator's JSON array of per-scene narrations.

    Returns the cleaned list on success, None if the model's output is
    unparseable, wrong-shaped, doesn't cover every scene exactly once, or
    contains a narration too thin to survive the pre-audio gate (< 18 tokens).
    All-or-nothing on purpose: a partial pass would leave some scenes with an
    empty narrator track while the (deterministic) fallback chain knew how to
    build the whole deck.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    block = re.search(r"```(?:json)?\s*(\[.+?\])\s*```", raw, re.DOTALL)
    text = block.group(1) if block else raw.strip()
    decoder = json.JSONDecoder()
    idx = 0
    obj: object | None = None
    while True:
        idx = text.find("[", idx)
        if idx == -1:
            return None
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        break
    if not isinstance(obj, list):
        return None
    narrations: list[dict] = []
    seen: set[int] = set()
    for item in obj:
        if not isinstance(item, dict):
            return None
        scene = item.get("scene")
        nar = item.get("narration")
        if not isinstance(scene, int) or not isinstance(nar, str):
            return None
        if not (1 <= scene <= expected):
            return None
        clean = _clean_narration(nar)
        if not clean or len(_nar_tokens(clean)) < _NARR_MIN_TOKENS + 6:
            return None
        narrations.append({"scene": scene, "narration": clean})
        seen.add(scene)
    if len(narrations) != expected or len(seen) != expected:
        return None
    return narrations


def _narrate_plan(plan: dict, content: str,
                  voice: NarrationVoice | None = None,
                  topics: list[str] | None = None) -> bool:
    """Phase-2 Pass B: dedicated narration pass over an existing lesson plan.

    The planner no longer writes narration (STUDIO_PROMPT leaves it empty); a
    SHORT, plan-scoped prompt asks the same free model for the full spoken
    track. Returns True when every scene got a gate-valid narration (>= the
    thin-repair floor). On ANY failure (unparseable, wrong count, a too-thin
    or empty scene) it returns False and the caller keeps the deterministic
    narration chain (openers, dedupe, deepen, rebuild) as the fallback - so a
    bad narrator can never block a build.
    """
    voice = voice or _MHE_VOICE
    scenes = plan.get("scenes") or []
    if not scenes:
        return False
    slim = [{
        "scene": i,
        "section": str(sc.get("section", "")),
        "title": str(sc.get("title", "")),
        "bullets": [str(b) for b in (sc.get("bullets") or [])],
        "steps": [str(s) for s in (sc.get("steps") or [])],
        "design_decision": str(sc.get("design_decision", "")),
    } for i, sc in enumerate(scenes, 1)]
    topics_block = ", ".join(str(t) for t in (topics or [])) if topics \
        else "the docs' central topics"
    prompt = (NARRATION_PROMPT.replace("{scene_count}", str(len(scenes)))
              + "\n\nPLAN (narration fields are blank - you write them):\n"
              + json.dumps(slim, ensure_ascii=False, indent=1)
              + f"\n\nLESSON TOPICS: {topics_block}"
              + "\n\nSOURCE EXCERPT (ground every fact here; never invent):\n"
              + _bounded_content(content, 3000))
    try:
        raw = _ask_llm_stable(prompt,
                              max_tokens=planner_output_budget(len(prompt)))
    except RuntimeError:
        return False
    narrations = _narrations_from_json(raw, expected=len(scenes))
    if narrations is None:
        return False
    for entry in narrations:
        scenes[entry["scene"] - 1]["narration"] = entry["narration"]
    return True
