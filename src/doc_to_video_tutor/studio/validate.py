"""guard_plan / review_plan and render-blocking narration gates."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import _SOFT_PREFIXES
from .narration import (
    _narration_repeat_report,
    _protected_terms,
    _unsafe_repeat_scenes,
    narration_matches_voice_policy,
)
from .plan import (
    _grounding_issues,
    _placeholder_titles,
    _repeated_bullets,
    _scene_count_problem,
    _section_problems,
)
from .speech import contains_corrupt_text, spoken_token_set
from .text import (
    _has_non_latin_script,
    _merged_token_issues,
    _nar_tokens,
    _near_dupe_bullets,
    _ngram_set,
)
from .topics import (
    _opening_is_on_topic,
    _opening_template_hit,
    _plan_is_on_topic,
    _topic_coverage_problem,
)
from .util import file_digest
from .voice import NarrationVoice, _make_voice


def _narration_integrity_problems(plan: dict) -> list[str]:
    """Post-repair sanity: no narration damaged by the repeat machinery.

    Empty narration, sub-minimum spoken length, or fragment artifacts mean the
    deterministic repair refused/left damage -> the plan must be regenerated
    (hard), never shipped and rendered.
    """
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        nar = str(sc.get("narration", ""))
        if not nar.strip():
            problems.append(f"scene {i}: empty narration")
            continue
        if len(_nar_tokens(nar)) < 6:
            problems.append(f"scene {i}: narration too short to speak "
                            f"({len(_nar_tokens(nar))} tokens) — repair damaged it")
        if contains_corrupt_text(nar):
            problems.append(
                f"scene {i}: narration has corrupt/mixed-script text (Devanagari "
                "mixed with Cyrillic/Arabic/CJK, control chars, or \ufffd) — "
                "not speakable")
        if not _has_teaching_content(sc):
            problems.append(
                f"scene {i}: transition-only/empty scene — no bullets, steps, "
                f"flow, or design decision and narration is filler "
                f"({len(_nar_tokens(nar))} tokens)")
        if re.match(r"[.,!?;:]", nar.strip()):
            problems.append(f"scene {i}: narration starts with punctuation")
        for frag in (". .", ". . .", "  ,", " .", ",."):
            if frag in nar:
                problems.append(f"scene {i}: fragment artifact {frag!r}")
                break
    return problems


_SLIDE_TEXT_FIELDS = ("title", "section", "bullets", "steps", "flow",
                      "analogy", "design_decision", "visual_diagram",
                      "code_snippet", "takeaways")


def _scene_metadata_problems(plan: dict) -> list[str]:
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        if not str(sc.get("topic", "")).strip():
            problems.append(f"scene {i}: missing topic metadata")
        refs = sc.get("source_refs")
        if (not isinstance(refs, list)
                or not any(str(value).strip() for value in refs)):
            problems.append(f"scene {i}: missing source_refs metadata")
        pages = sc.get("bullet_pages")
        if isinstance(pages, list) and pages:
            flat = [str(value) for page in pages
                    if isinstance(page, list) for value in page]
            bullets = [str(value) for value in (sc.get("bullets") or [])]
            if flat != bullets:
                problems.append(
                    f"scene {i}: bullet_pages does not match bullets")
            elif any(len(page) > 4 for page in pages
                     if isinstance(page, list)):
                problems.append(f"scene {i}: bullet_pages exceeds 4 items")
    return problems


def _slide_text_language_problems(plan: dict) -> list[str]:
    """Slide visible text must stay ENGLISH (Latin script).

    v011_006 shipped 4 Devanagari slide items (3 bullets + 1 analogy) through
    every gate: ``contains_corrupt_text`` only sees MIXED scripts, and the
    grounding ``_anchored`` shortcut treats <2-Latin-token text as "anchored",
    so PURE non-Latin invention was invisible to both. This mirrors the
    narration-corrupt precedent: hard FAIL so the plan is honestly rejected /
    resampled instead of rendering Marathi onto slides.
    """
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        for field in _SLIDE_TEXT_FIELDS:
            value = sc.get(field, "")
            values = value if isinstance(value, list) else [value]
            for item_index, item in enumerate(values, 1):
                text = str(item)
                if _has_non_latin_script(text):
                    suffix = f"[{item_index}]" if isinstance(value, list) else ""
                    problems.append(
                        f"scene {i} SlideTextNotEnglish: non-Latin script in "
                        f"{field}{suffix}: {text[:64]!r}")
    for field in ("title", "opening"):
        text = str(plan.get(field, ""))
        if _has_non_latin_script(text):
            problems.append(
                f"plan {field} SlideTextNotEnglish: non-Latin script: "
                f"{text[:64]!r}")
    for j, t in enumerate(plan.get("takeaways") or [], 1):
        if _has_non_latin_script(str(t)):
            problems.append(
                f"takeaway {j} SlideTextNotEnglish: non-Latin script: "
                f"{str(t)[:64]!r}")
    return problems


_TRANSITION_FILLER = frozenset(
    {w for w in (
        "ab aur ye yah yo to bhi hi hai hain ho ki ke ko ka kar karke se mein "
        "par per ne na is isse us iska iske uska uske kehta kehte chaliye dekhte "
        "samjhenge aage badhte badta taraf lekin phir the and of to in on at for "
        "with that this from into onto some then where here when") if len(w) >= 3})


def _has_teaching_content(sc: dict) -> bool:
    """A scene must carry real lesson material, not a bare transition.

    Mirrors the reviewer rule: reject scenes with fewer than 2 bullets or
    purely transitional text. Any slide substance (bullets/steps/flow/analogy
    or a design decision) makes the scene speakable; otherwise the narration
    alone must carry a real informational core (content-word density).
    """
    if len(sc.get("bullets") or []) >= 2:
        return True
    if sc.get("steps") or sc.get("flow") or sc.get("analogy"):
        return True
    if str(sc.get("design_decision", "")).strip():
        return True
    return _narration_has_content(
        str(sc.get("narration", "")), str(sc.get("title", "")))


def _narration_has_content(nar: str, title: str) -> bool:
    """True when a narration without slide substance still teaches: it must be
    long enough and dominated by non-title, non-filler words. A Scene-3-style
    'Ab aage badhte hain <title> ki taraf' echo scores ~0 content."""
    title_words = {w for w in re.sub(r"[\u2192:()\d]", " ", title)
                   .casefold().split() if len(w) >= 3}
    tokens = [w.strip(".,;:!?") for w in nar.casefold().split()]
    tokens = [w for w in tokens if len(w) >= 3]
    if len(tokens) < 8:
        return False
    content = [w for w in tokens
               if w not in title_words and w not in _TRANSITION_FILLER]
    return len(content) >= 3 and len(content) / len(tokens) >= 0.4
def check_audit_binding(plan_path: Path, audit_path: Path) -> list[str]:
    """Verify an audit artifact describes the plan sitting beside it.

    An audit is only evidence if it is bound to the exact bytes it audited. The
    previous audit recorded `plan: str(src)` - the *input* path - while shipping
    beside the *repaired* plan, so nothing tied the two together and a renamed or
    replaced file inherited the audit's clean bill of health. The digest is the
    identity; the path is display metadata and is never trusted for that.

    Returns a list of findings; empty means the audit is bound correctly. An
    audit with no digest at all is reported rather than assumed good, because
    silently accepting an unbound audit would defeat the point of the check.
    """
    findings: list[str] = []
    try:
        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"audit unreadable: {exc}"]
    recorded = str(audit.get("plan_sha256") or "")
    if not recorded:
        findings.append("audit_plan_digest_missing: the audit carries no "
                        "plan_sha256, so it cannot be tied to any plan")
        return findings
    actual = file_digest(Path(plan_path))
    if not actual:
        findings.append(f"audit_plan_unreadable: cannot digest {plan_path}")
    elif actual != recorded:
        findings.append(
            f"audit_plan_digest_mismatch: the audit was written for a different "
            f"plan than the one beside it (recorded {recorded[:12]}..., on disk "
            f"{actual[:12]}...); the audit does not describe this plan")
    return findings


def _unspoken_visual_claims(plan: dict,
                           voice: NarrationVoice | None = None) -> list[str]:
    """Structured visual facts the scene shows but the narration never names.

    Only structured blocks are checked - status badges and JSON payload keys -
    because those are mechanically decidable. A judgement call like "is this
    decorative element earning its place" is deliberately not automated: it has
    no decidable form, and a gate that cannot be computed reliably produces
    false positives that train everyone to ignore the gate.

    This exists because the enrichment that puts a five-value exit-code legend on
    screen also created the risk that four of those values are never spoken. On
    `mod03_gates_v012_002` the badge row showed `0 PASS` through `4 CONFIG ERR`
    while the narration mentioned only 3 and 4, so the viewer reads three facts
    the lesson never explains. Soft, not hard: the remedy is a narration or
    enrichment decision, and silently rewriting the script to enumerate a legend
    is exactly the kind of content injection the narration contract forbids.
    """
    findings: list[str] = []
    rules = tuple(voice.pronunciation_rules) if voice is not None else None
    for index, scene in enumerate(plan.get("scenes", []), 1):
        if not str(scene.get("narration", "")).strip():
            continue
        # Compare in the spoken alphabet: the badge says "3" and the narrator
        # says "exit three", so a raw substring test reports a spoken fact as
        # unspoken. See `spoken_token_set`.
        spoken = spoken_token_set(str(scene["narration"]), rules)
        unspoken: list[str] = []
        for badge in (scene.get("status_badges") or []):
            if not isinstance(badge, dict):
                continue
            code = str(badge.get("code", "")).strip()
            label = str(badge.get("label", "")).strip()
            code_hit = bool(spoken_token_set(code, rules) & spoken)
            label_hit = bool(spoken_token_set(label, rules) & spoken)
            if not code_hit and not label_hit:
                unspoken.append(f"{code} {label}".strip())
        snippet = scene.get("json_snippet")
        if isinstance(snippet, str) and snippet.strip():
            for key in _json_keys(snippet):
                if not (spoken_token_set(key, rules) & spoken):
                    unspoken.append(key)
        if unspoken:
            findings.append(
                f"scene {index} unspoken visual claim: the slide shows "
                f"{', '.join(unspoken[:6])} but the narration never mentions "
                f"them; the viewer reads facts the lesson does not explain")
    return findings


def _json_keys(snippet: str) -> list[str]:
    """Top-level-ish object keys in a JSON payload, without a parse requirement.

    The snippet arrives as rendered text, not a parsed object, and it is allowed
    to be a fragment. A regex over quoted keys is enough for a coverage check and
    cannot fail on input a real parse would reject.
    """
    import re as _re

    return [m.group(1) for m in _re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:',
                                            snippet)][:6]


def _render_blocking_problems(plan: dict,
                              voice: NarrationVoice | None = None,
                              protected: frozenset[str] | None = None
                              ) -> list[str]:
    """Hard, pre-render audit: problems that MUST be zero before TTS/render.

    Deterministic only (no LLM): recompute the repeat gate from the plan's own
    content, so a plan that shipwrecks the free 7B never reaches audio/video
    with a banned narration phrase, an unrepairable repeat, damaged narration,
    or an out-of-range scene count. Each finding is actionable and names the
    exact offending scene(s)/phrase(s) so the fix is mechanical.
    """
    protected = _protected_terms(plan) if protected is None else protected
    banned, _ = _narration_repeat_report(plan, protected)
    problems: list[str] = []
    if banned:
        problems.append(f"RENDER-BLOCKING: {len(banned)} banned narration "
                        f"phrase(s) unresolved: {sorted(banned)[:4]}"
                        + (" ..." if len(banned) > 4 else ""))
    unsafe = _unsafe_repeat_scenes(plan, protected)
    if unsafe:
        problems.append("RENDER-BLOCKING: unrepairable narration repeat "
                        f"(scene(s) {','.join(str(i) for i in unsafe)})")
    problems += _narration_integrity_problems(plan)
    problems += _scene_metadata_problems(plan)
    problems += _slide_text_language_problems(plan)
    pc = _scene_count_problem(plan)
    if pc:
        problems.append(f"RENDER-BLOCKING: {pc}")
    return problems
def guard_plan(plan: dict, topics: list[str],
               source_bigrams: list[str] | None = None,
               source_tokens: list[str] | None = None,
               source_top: list[str] | None = None,
               voice: NarrationVoice | None = None) -> list[str]:
    """Return a list of problems found in a lesson plan (empty = PASS)."""
    problems = []
    passed, _reason = narration_matches_voice_policy(plan, voice)
    if not passed:
        problems.append("narration pure-English")
    if topics and not _plan_is_on_topic(plan, topics):
        problems.append("topic drift")
    if topics and not _opening_is_on_topic(plan, topics):
        problems.append("opening drifted")
    protected = _protected_terms(plan)
    banned, _prot = _narration_repeat_report(plan, protected)
    unsafe = _unsafe_repeat_scenes(plan, protected)
    if unsafe:
        problems.append("unrepairable narration repeat (scene(s) "
                        + ",".join(str(i) for i in unsafe) + ")")
    elif banned:
        problems.append("repeating narration phrase")
    problems += _narration_integrity_problems(plan)
    problems += _scene_metadata_problems(plan)
    problems += _slide_text_language_problems(plan)
    problems += _unspoken_visual_claims(plan, voice)
    if _opening_template_hit(plan):
        problems.append("opening reuses prompt example sentence")
    if source_top:
        cov = _topic_coverage_problem(plan, source_top)
        if cov:
            problems.append(cov)
    problems += _section_problems(plan)
    rb = _repeated_bullets(plan)
    if rb:
        problems.append(f"copy-paste bullets reused across scenes: {len(rb)} item(s)")
    nd = _near_dupe_bullets(plan)
    if nd:
        problems.append(f"near-duplicate bullets vs takeaways: {len(nd)} item(s)")
    pt = _placeholder_titles(plan)
    if pt:
        problems.append(f"placeholder title restates section: {len(pt)} scene(s)")
    ft = _merged_token_issues(plan)
    if ft:
        problems.append(f"fused slide token(s) unreadable: {len(ft)} item(s)")
    pc = _scene_count_problem(plan)
    if pc:
        problems.append(pc)
    if source_bigrams and source_tokens:
        ngrams = _ngram_set(source_bigrams)
        issues = _grounding_issues(plan, ngrams, set(source_tokens))
        if issues:
            problems.append(f"ungrounded slide text: {len(issues)} item(s)")
    return problems
def review_plan(path: Path, topics: list[str] | None = None) -> int:
    """Audit a saved .plan.json and print a verdict. Exit 0 = PASS, 1 = issues."""
    data = json.loads(path.read_text(encoding="utf-8"))
    plan = data.get("plan", data)
    saved = data.get("topics", [])
    ref = topics or saved
    src_bg = data.get("source_bigrams")
    src_tk = data.get("source_tokens")
    src_top = data.get("source_top")
    voice = _make_voice(data.get("voice") or (data.get("narration_voice") or {}).get("name"))
    problems = guard_plan(plan, ref, source_bigrams=src_bg, source_tokens=src_tk,
                          source_top=src_top, voice=voice)
    hard = [p for p in problems if not any(p.startswith(s) for s in _SOFT_PREFIXES)]
    soft = [p for p in problems if any(p.startswith(s) for s in _SOFT_PREFIXES)]
    protected = _protected_terms(plan)
    _banned, _prot = _narration_repeat_report(plan, protected)
    ungrounded = (_grounding_issues(plan, _ngram_set(src_bg),
                                    set(src_tk)) if src_bg and src_tk else [])
    print(f"Reviewing {path}")
    print(f"  title     : {plan.get('title')!r}")
    print(f"  voice     : {voice.name}")
    print(f"  scenes    : {len(plan.get('scenes', []))}")
    print(f"  protected repeats: {len(_prot)}")
    if ref:
        print(f"  topics    : {', '.join(ref)}")
    if hard:
        print("  VERDICT   : ISSUES")
        for p in hard:
            print(f"    - {p}")
        for d in ungrounded:
            print(f"      {d}")
        return 1
    if soft:
        print("  VERDICT   : PASS (soft warnings)")
        for p in soft:
            print(f"    ~ {p}")
        return 0
    print("  VERDICT   : PASS")
    return 0
