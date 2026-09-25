"""Spoken-script normalization, TTS audit and the tts_script.json artifact.

The plan carries WRITTEN narration (Latin-script Hinglish, slide-oriented
titles, code tokens like 'M1', 'Active.json', '='). That text is written for
the eye. Edge TTS needs a SPOKEN layer: symbol tokens expanded to words, slide
metadata stripped, stray quotes/brackets repaired, titles mentioned once.

Nothing here edits the plan narration - the original stays in
``original_narration`` and only ``spoken`` (the exact edge-tts input) is
requested by synth_scenes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import _nar_tokens
from .voice import NarrationVoice, PronunciationRule

_SENT_SPLIT = re.compile(r"(?<=[.!?\u0964\u0965])\s+")
_TERMINAL = ".!?\u0964\u0965"
_NUM_WORDS = ("zero", "one", "two", "three", "four",
              "five", "six", "seven", "eight", "nine")
_QUOTE_KEEP = re.compile(r"(?<=\w)['\u2019](?=\w)")
_QUOTE_STRAY = re.compile(r"[\u2018\u2019'\"]")
_DQUOTE = re.compile(r"\"")
_TOKEN = re.compile(r"[A-Za-z0-9_]")
_BRACKET_PAIR = re.compile(r"\(\s*([\w .,;&-]+?)\s*\)")
# Script/corruption detectors (rule TTS_TEXT_CORRUPTED, language policy).
_CORRUPT_CONTROL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_CJK = re.compile(r"[\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF]")
_FULLWIDTH = re.compile(r"[\uFF00-\uFFEF]")
_ARROW = re.compile(r"[→←⇒↔]")
_SLIDE_NUM = re.compile(r"(?<!\w)\d{1,2}[.:]\s*")
_RAW_FILE = re.compile(r"(?<!\w)[\w./-]+\.(?:json|yml|yaml|py|md|txt)\b", re.I)
_CAPITAL = re.compile(r"^[A-Z][A-Za-z]{3,}$")
# Common capitalized sentence/opener words that must never trip the unknown-token gate.
_KNOWN_CAPS = frozenset({
    "the", "and", "this", "that", "these", "those", "but", "then",
    "first", "second", "third", "next", "is", "it", "key", "main",
    "how", "what", "why", "when", "which", "here", "so",
    "chaliye", "ab", "aur", "yahan", "ismein", "to", "reason", "example",
})


def _known_terms(plan: dict, voice: NarrationVoice) -> frozenset[str]:
    """Lowest-risk allowlist for the unknown-token gate: anything anchored to the
    plan's own vocabulary (titles, takeaways, topics, protected grams/entities,
    saved source terms) or to the voice's pronunciation rules. A capitalized
    mid-sentence token outside this set is a likely 7B hallucination."""
    known: set[str] = set(_KNOWN_CAPS)
    for r in voice.pronunciation_rules:
        known.update(str(r.written).lower().split())
        known.update(str(r.spoken).lower().split())
    for phrase in (list(voice.openers) + list(voice.closers)
                   + list(voice.dd_leads) + list(voice.narr_heads)
                   + [voice.takeaways_lead, voice.takeaways_any_lead]):
        words = str(phrase).split()
        if words:
            known.add(words[0].strip(":.,;").lower())
    known.update(["reason", "iska", "kaaran", "design", "decision"])
    texts = [str(t) for t in (plan.get("takeaways") or [])]
    texts += [str(plan.get("title", "")), str(plan.get("opening", ""))]
    for sc in plan.get("scenes", []):
        texts.append(str(sc.get("title", "")))
        texts.append(str(sc.get("design_decision", "")))
        texts += [str(t) for t in (sc.get("bullets") or [])]
    for text in texts:
        for w in str(text).split():
            wl = w.strip(".,;:!?()[]{}\u201c\u201d").lower()
            if re.match(r"^[a-z0-9]+$", wl):
                known.add(wl)
    for key in ("protected_trigrams", "protected_entities", "source_top",
                "source_tokens", "topics"):
        for item in (plan.get(key) or []):
            for w in str(item).replace("-", " ").split():
                wl = w.strip(".,;:!?\u201c\u201d").lower()
                if len(wl) >= 3:
                    known.add(wl)
    return frozenset(known)


_NUMBER_WORD_DIGITS = {word: str(i) for i, word in enumerate(_NUM_WORDS)}


def spoken_token_set(text: str, rules: tuple | None = None) -> frozenset[str]:
    """Tokens of ``text`` in the form a comparison against spoken text needs.

    A PowerPoint badge says ``3`` while the narrator says "exit three", so a
    naive token comparison reports a fact as unspoken when it is being spoken.
    That is the same asymmetry the reveal matcher had to solve, and it is solved
    the same way: put both sides in one alphabet before comparing, by folding
    number-words back to digits and applying the profile's spoken-form
    expansion. Applied here it stops the unspoken-claim gate from crying wolf on
    every numeric badge in the deck.
    """
    body = str(text)
    if rules:
        body = speech_expand(body, rules)
    # IGNORECASE means the match can be "Zero", so the lookup has to fold case
    # too - a sentence-initial "Zero means pass" would otherwise raise KeyError.
    folded = re.sub(r"\b(" + "|".join(_NUMBER_WORD_DIGITS) + r")\b",
                    lambda m: _NUMBER_WORD_DIGITS[m.group(1).lower()], body,
                    flags=re.IGNORECASE)
    return frozenset(_nar_tokens(folded))


def contains_suspicious_script_mix(text: str) -> bool:
    """Devanagari + any unexpected script (Cyrillic/Arabic/CJK/fullwidth) in one
    string is never valid in the Latin-only or Latin+Devanagari MHE profiles."""
    dev = bool(_DEVANAGARI.search(text))
    if not dev:
        return False
    return bool(_CYRILLIC.search(text) or _ARABIC.search(text)
                or _CJK.search(text) or _FULLWIDTH.search(text))


def contains_corrupt_text(text: str) -> bool:
    """Replacement char, control chars, or an unsupported/mixed script run."""
    return ("\ufffd" in text or bool(_CORRUPT_CONTROL.search(text))
            or bool(_CYRILLIC.search(text)) or bool(_ARABIC.search(text))
            or bool(_CJK.search(text)) or contains_suspicious_script_mix(text))


@dataclass(frozen=True)
class TtsFinding:
    code: str
    severity: str  # "FAIL" | "WARN"
    message: str


_SNAKE_CASE_RE = re.compile(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])")


def expand_snake_case(text: str) -> str:
    """Speak unmapped snake_case identifiers as separate words.

    Pronunciation rules are an explicit dictionary, so any identifier the model
    invents that is not in it (``baseline_report``, ``golden_set_path``, ...)
    keeps its underscore and reaches the voice as a code fragment. Rather than
    adding a rule per identifier, the generic fallback runs after the dictionary
    and splits whatever is left: ``baseline_report.json`` becomes
    "baseline report dot json". Runs after rule expansion so a mapped identifier
    such as ``schema_version`` is already spoken and never reaches this pass.
    """
    return _SNAKE_CASE_RE.sub(" ", text)


def speech_expand(text: str, rules: tuple[PronunciationRule, ...]) -> str:
    """Expand written tokens to speakable words (longest match first)."""
    ordered = sorted(((r.written, r.spoken, r.word_boundary) for r in rules),
                     key=lambda item: len(item[0]), reverse=True)
    if not ordered:
        return expand_snake_case(text)
    spoken_by_group: dict[str, str] = {}
    parts: list[str] = []
    for idx, (w, s, wb) in enumerate(ordered):
        quoted = r"(?<!\w)" + re.escape(w) + r"(?!\w)" if wb else re.escape(w)
        parts.append(f"({quoted})")
        spoken_by_group[str(idx)] = s
    combined = re.compile("|".join(parts), re.IGNORECASE)
    def _repl(m: re.Match[str]) -> str:
        idx = m.lastindex
        return spoken_by_group[str(idx - 1)] if idx is not None else m.group(0)
    return expand_snake_case(combined.sub(_repl, text))


def _flatten_parentheses(text: str) -> str:
    text = _BRACKET_PAIR.sub(lambda m: m.group(1).strip(), text)
    text = text.replace("(", " (").replace(")", ") ")
    text = re.sub(r"[({\[}\])]", " ", text)
    text = text.replace("`", " ").replace("'", "").replace("\u201c", " ")
    text = text.replace("\u2014", ", ").replace("\u2013", ", ")
    text = re.sub(r"(?<!\w)\d{1,2}[.):]\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=\d)\s*\.\s*(?=[A-Za-z\u0900-\u097F])", ", ", text)
    return text


def strip_slide_meta(text: str, phrases: tuple[str, ...]) -> tuple[str, int]:
    hits = 0
    for phrase in phrases:
        low = text.casefold()
        while phrase.casefold() in low:
            idx = low.index(phrase.casefold())
            text = text[:idx].rstrip() + " " + text[idx + len(phrase):].lstrip()
            low = text.casefold()
            hits += 1
    return text.strip(), hits


def collapse_repeated_title(text: str, title: str) -> tuple[str, int]:
    """Drop every occurrence of a title after the first (title mentioned once)."""
    needle = title.strip().rstrip(" .,!?;:")
    title_c = needle.casefold()
    if not title_c or len(title_c) < 3:
        return text, 0
    low = text.casefold()
    count = low.count(title_c)
    if count <= 1:
        return text, 0
    first = low.find(title_c)
    rest = text[first + len(needle):]
    low_rest = rest.casefold()
    removed = 0
    while title_c in low_rest:
        idx = low_rest.index(title_c)
        rest = rest[:idx].rstrip() + " " + rest[idx + len(needle):].strip()
        low_rest = rest.casefold()
        removed += 1
    return (text[:first + len(needle)] + rest).strip(), removed


def repair_speech_punctuation(text: str) -> tuple[str, int]:
    text = _DQUOTE.sub("", text)
    text = _QUOTE_STRAY.sub("", text)
    text = text.replace("...", ". ")
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"(?<=[.!?])\s*(?=[.!?])", " ", text)
    text = re.sub(r"([.!?])((?:\s*[?!:;]+)+)", r"\1 ", text)
    text = re.sub(r"\s+:\s*", " ", text)
    text = re.sub(r"\s*:\s+", " ", text)
    text = re.sub(r"(?<!\s):(?=\S)", ": ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s*-\s*(?=[,.;:!?]|$)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text and not text.endswith(tuple(_TERMINAL)):
        text += "."
    return text, 0


def _spoken_variant(voice: NarrationVoice, written: str) -> str:
    text, _hits = strip_slide_meta(written, voice.spoken_meta_leaks)
    text = _flatten_parentheses(text)
    text = speech_expand(text, voice.pronunciation_rules)
    text, _fixed = repair_speech_punctuation(text)
    return text


_HINDI_PARTICLES = ("ki", "ka", "ke", "ko", "se", "aur", "par", "vala",
                    "wali", "mein", "to", "tak", "bhi", "hi")


def _fused_particle_tokens(spoken: str, known: frozenset[str]) -> dict[str, str]:
    """Lowercase tokens that are a Hindi particle fused onto an English word.

    `tts_unknown_token` only inspects capitalised tokens, so a lowercase fusion
    is invisible to it. The real lesson contained "kideterministic" - the model
    wrote it, the source never contained it, and four repair passes preserved it
    verbatim, because nothing in the pipeline looks at unknown lowercase tokens.

    Detection is deliberately narrow, because a general "unknown token" rule
    floods technical narration with false positives. Both halves must be
    independently attested, which is what makes it safe:

      1. the whole token is NOT known vocabulary, so it cannot be a real domain
         term that merely looks fused;
      2. some prefix is a known Hindi/Hinglish particle;
      3. the remaining suffix IS known vocabulary, i.e. a word the plan itself
         uses elsewhere. A fusion leaves a tail that the narrator went on to
         pronounce normally ("...kideterministic..." then "fully deterministic").

    Condition 3 is the load-bearing one. Without it, "killed" and "together"
    split attractively and the rule cries wolf on ordinary English.

    Returns {fused_token: "particle suffix"}; empty when nothing qualifies.
    """
    out: dict[str, str] = {}
    for raw in spoken.split():
        bare = raw.strip(".,;:!?()[]{}\"'“”").casefold()
        if len(bare) < 8 or bare in known:
            continue
        for particle in _HINDI_PARTICLES:
            if not bare.startswith(particle):
                continue
            suffix = bare[len(particle):]
            if len(suffix) >= 5 and suffix in known:
                out[bare] = f"{particle} {suffix}"
                break
    return out


def _word_count(text: str) -> int:
    return len(str(text).split())


def _join_sentence_items(items: list[str]) -> str:
    """Join slide bullets into spoken sentences: each item gets its own
    terminating punctuation instead of running into the next bullet."""
    parts: list[str] = []
    for item in items:
        text = str(item).strip().strip(_TERMINAL)
        if text.lower().startswith("key takeaways"):
            text = text[len("key takeaways"):].lstrip(": ")
        if text:
            parts.append(text + ".")
    return " ".join(parts)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]

def _norm_said_tokens(text: str) -> tuple[str, ...]:
    """Canonical word tokens for comparing a spoken sentence to the spoken title
    head: lowercase, punctuation dropped, number words mapped back to digits and
    leading zeros stripped ('02' -> '2') so a digit-bearing title compares equal
    to its expanded spoken head."""
    out: list[str] = []
    for w in re.findall(r"[A-Za-z0-9]+", str(text).casefold()):
        if w in _NUM_WORDS:
            w = str(_NUM_WORDS.index(w))
        w = w.lstrip("0") or "0"
        out.append(w)
    return tuple(out)


def audit_tts_script(clips: list[dict], voice: NarrationVoice,
                     plan: dict | None = None) -> list[TtsFinding]:
    findings: list[TtsFinding] = []
    if not voice.tts_voice:
        findings.append(TtsFinding(
            "tts_voice_unset", "FAIL",
            "voice profile has no concrete tts_voice; the audio is not reproducible"))
    known = _known_terms(plan or {}, voice)
    for clip in clips:
        role = clip.get("role", "scene")
        tag = f"{role}:{clip.get('index', '?')}"
        spoken = str(clip.get("spoken", ""))
        title = str(clip.get("title", ""))
        for phrase in voice.spoken_meta_leaks:
            if phrase.casefold() in spoken.casefold():
                findings.append(TtsFinding("tts_slide_meta_leak", "FAIL",
                                           f"{tag} residual slide-metadata "
                                           f"{phrase!r} in spoken text"))
                break
        if contains_corrupt_text(spoken):
            findings.append(TtsFinding(
                "tts_text_corrupted", "FAIL",
                f"{tag} corrupt/mixed-script text is not speakable: {spoken!r}"))
        title_probe = title.strip().rstrip(" .,!?;:").casefold()
        if title_probe and spoken.casefold().count(title_probe) > 1:
            findings.append(TtsFinding("tts_title_repeated", "WARN",
                                       f"{tag} title mentioned more than once"))
        residual = _ARROW.search(spoken) or _SLIDE_NUM.search(spoken) \
            or _RAW_FILE.search(spoken)
        if residual:
            findings.append(TtsFinding(
                "tts_symbol_heavy", "FAIL",
                f"{tag} raw slide symbol/number/file token remains: {residual.group(0)!r}"))
        elif re.search(r"[{}\[\]_=]|(?<!\w)\d+/\d+", spoken):
            findings.append(TtsFinding("tts_code_fragment", "WARN",
                                       f"{tag} unexpanded code/symbol token remains"))
        unbalanced = (spoken.count("(") != spoken.count(")")
                      or spoken.count("[") != spoken.count("]"))
        if unbalanced:
            findings.append(TtsFinding("tts_punctuation_error", "FAIL",
                                       f"{tag} unbalanced bracket"))
        for i, s in enumerate(sentences(spoken), 1):
            if role == "final":
                break
            spoken_head = str(clip.get("spoken_title", ""))
            head_norm = _norm_said_tokens(spoken_head)
            s_norm = _norm_said_tokens(s)
            is_head = (i == 1 and bool(head_norm) and 1 <= len(s_norm) <= 8
                       and head_norm[: len(s_norm)] == s_norm)
            if _is_slide_fragment(s, title) and not is_head:
                findings.append(TtsFinding(
                    "tts_sentence_fragment", "FAIL",
                    f"{tag} sentence {i} is a bare title/bullet fragment: {s!r}"))
        unknown = _unexpected_capital_tokens(spoken, known)
        if unknown:
            findings.append(TtsFinding(
                "tts_unknown_token", "FAIL",
                f"{tag} unexplained token not in title/source/glossary: "
                f"{', '.join(sorted(unknown))}"))
        fused = _fused_particle_tokens(spoken, known)
        if fused:
            findings.append(TtsFinding(
                "tts_token_fusion", "WARN",
                f"{tag} token(s) look like a Hindi particle fused onto an "
                f"English word, absent from the source: "
                f"{', '.join(f'{k} -> {v}' for k, v in sorted(fused.items()))}"))
        if role == "final":
            continue
        wc = _word_count(spoken)
        if wc < 20:
            findings.append(TtsFinding("tts_too_short_critical", "FAIL",
                                       f"{tag} only {wc} spoken words (min 20)"))
        elif wc < 25:
            findings.append(TtsFinding("tts_too_short", "WARN",
                                       f"{tag} only {wc} spoken words (min 25)"))
        elif wc > 90:
            findings.append(TtsFinding("tts_too_long_critical", "FAIL",
                                       f"{tag} {wc} spoken words (max ~90)"))
        elif wc > 70:
            findings.append(TtsFinding("tts_too_long", "WARN",
                                       f"{tag} {wc} spoken words (max ~70)"))
        for s in sentences(spoken):
            if _word_count(s) > 30:
                findings.append(TtsFinding("tts_long_sentence", "WARN",
                                           f"{tag} sentence of {_word_count(s)} words"))
        if not _has_teaching_claim(clip, voice, plan):
            findings.append(TtsFinding(
                "tts_required_concept_missing", "FAIL",
                f"{tag} narration is transition/title-only with no teaching content"))
    return findings


def _is_slide_fragment(s: str, title: str) -> bool:
    """A sentence that is basically a restated slide title/bullet (no verb weight)."""
    words = s.strip().strip(_TERMINAL).split()
    if not 2 <= len(words) <= 6:
        return False
    title_words = set(re.sub(r"[\u2192:()\d]", " ", title).casefold().split())
    if not title_words:
        return False
    overlapped = sum(1 for w in words
                     if w.strip().strip(".,;:!?").casefold() in title_words)
    return overlapped / len(words) >= 0.6


def _hydrate_spoken_fragments(spoken: str, title: str, spoken_title: str,
                              voice: NarrationVoice, scene_index: int = 1) -> str:
    """Wrap bare title/bullet-fragment sentences with a spoken Layer-B lead.

    The pre-audio gate refuses 2-6 word sentences whose words are >=60% slide
    title words (``_is_slide_fragment``): a bullet whose vocabulary overlaps its
    own scene title (e.g. scene 2 'Compare the results against the baseline.'
    vs 'Baseline snapshot & compare - the core loop') reads as a bare title
    echo when spoken. Bullets MUST stay verbatim in the spoken track (A2.13
    '2+ teaching points' contract), so instead of dropping them, prepend a
    rotating ``voice.bullet_leads`` carrier. A >=3-word carrier dissolves every
    realistic fragment (6 words, even 5/6 overlap -> 5/9 = 0.56 < 0.60), and
    doing it HERE - where ``audit_tts_script`` reads ``clip['spoken']`` -
    covers every narration source (LLM pass, deterministic rebuild, verify
    repair) with one change. Grammar lives only in Layer-B voice constants.
    """
    if not voice.bullet_leads or not title:
        return spoken
    head_norm = _norm_said_tokens(spoken_title)
    out: list[str] = []
    for k, s in enumerate(sentences(spoken), 1):
        s_norm = _norm_said_tokens(s)
        is_head = (k == 1 and bool(head_norm) and 1 <= len(s_norm) <= 8
                   and head_norm[: len(s_norm)] == s_norm)
        if _is_slide_fragment(s, title) and not is_head:
            lead = voice.bullet_leads[(scene_index + k)
                                      % len(voice.bullet_leads)].strip(" ,")
            s = f"{lead}, {s}"
        out.append(s)
    return " ".join(out)


def _unexpected_capital_tokens(spoken: str, known: frozenset[str]) -> set[str]:
    """Capitalized words mid-sentence that the plan never anchored -> likely 7B
    hallucinations (e.g. 'Prahlad'). Words starting a sentence are exempt."""
    out: set[str] = set()
    tokens = spoken.split()
    prev = ""
    for w in tokens:
        bare = w.strip(".,;:!?()[]{}'\u201c\u201d")
        sentence_start = not prev or prev.endswith((".", "!", "?"))
        if (not sentence_start and _CAPITAL.match(bare)
                and bare.casefold() not in known
                and bare.casefold() != "is"):
            out.add(bare)
        prev = w
    return out


def _has_teaching_claim(clip: dict, voice: NarrationVoice,
                        plan: dict | None = None) -> bool:
    """After removing opener/closer fridge-chatter and the title echo, is there
    still a real spoken claim? Catches Scene-3-style transition-only clips.

    Passes when (a) >=8 distinct non-title content words are spoken, or (b) the
    spoken track carries >=2 DISTINCT scene bullets verbatim - the A2.13
    '2+ teaching points' contract, which is the honest signal for a
    title-shaped topic ('Kind / Direction / Tolerance') whose terms legitimately
    match the slide title and would otherwise be over-excluded.
    """
    spoken = str(clip.get("spoken", ""))
    title_words = set(re.sub(r"[\u2192:()\d]", " ", str(clip.get("title", "")))
                      .casefold().split())
    for phrase in list(voice.openers) + list(voice.closers) + list(voice.narr_heads):
        spoken = spoken.casefold().replace(str(phrase).casefold(), " ")
    content = [w for w in spoken.split()
               if len(w) >= 3 and w.strip(".,;:!?").casefold() not in title_words]
    if len(content) >= 8:
        return True
    if not plan:
        return False
    idx = int(clip.get("index", 0))
    scenes = plan.get("scenes") or []
    if not (1 <= idx <= len(scenes)):
        return False
    bullets = [str(b) for b in (scenes[idx - 1].get("bullets") or []) if str(b).strip()]
    if len(bullets) < 2:
        return False

    def _norm(text: str) -> str:
        text = re.sub(r"[^\w' ]", " ", text).casefold()
        for _d in range(10):
            text = text.replace(f" {_NUM_WORDS[_d]} ", f" {_d} ")
        return " ".join(text.split())
    spoken_f = _norm(spoken)
    hits = sum(1 for b in bullets[:5] if _norm(b) in spoken_f)
    return hits >= 2


def build_tts_script(plan: dict, voice: NarrationVoice) -> dict:
    clips: list[dict] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        title = str(sc.get("title", "")).strip()
        narration = str(sc.get("narration", "")).strip()
        spoken_narration, _m = strip_slide_meta(narration, voice.spoken_meta_leaks)
        spoken_title, _t = strip_slide_meta(title, voice.spoken_meta_leaks)
        slide_meta_hits = _m + _t
        spoken_title = re.sub(r"^\s*\d+[.)]\s*", "", spoken_title).strip()
        spoken_title = speech_expand(spoken_title, voice.pronunciation_rules)
        spoken_title = _flatten_parentheses(spoken_title)
        spoken_title = repair_speech_punctuation(spoken_title)[0]
        spoken = (spoken_title + ". " if spoken_title else "") + _spoken_variant(
            voice, spoken_narration)
        spoken = repair_speech_punctuation(spoken)[0]
        if spoken_title:
            spoken, _removed = collapse_repeated_title(spoken, spoken_title)
        # Speak bare title/bullet fragments as prose (the pre-audio gate's
        # tts_sentence_fragment reads clip["spoken"], so hydration happens here).
        spoken = _hydrate_spoken_fragments(spoken, title, spoken_title, voice, i)
        clips.append({
            "role": "scene",
            "index": i,
            "section": str(sc.get("section", "")),
            "title": title,
            "narration": narration,
            "spoken_title": spoken_title,
            "spoken": spoken,
            "slide_meta_hits": slide_meta_hits,
            "word_count": _word_count(spoken),
        })
    takeaways = (plan.get("takeaways") or [])[:6]
    if takeaways:
        joined = _join_sentence_items(takeaways)
        final_spoken = _spoken_variant(voice, "Key takeaways. " + joined)
        clips.append({
            "role": "final",
            "index": len(plan.get("scenes", [])) + 1,
            "title": "Key takeaways",
            "narration": joined,
            "spoken_title": "",
            "spoken": final_spoken,
            "word_count": _word_count(final_spoken),
        })
    findings = audit_tts_script(clips, voice, plan)
    return {
        "schema_version": 1,
        "voice_profile": voice.name,
        "tts_voice": voice.tts_voice or "UNSET",
        "provider": "edge-tts",
        "rate": voice.rate,
        "pitch": voice.pitch,
        "volume": voice.volume,
        "pronunciation_rules": len(voice.pronunciation_rules),
        "audit": [{"code": f.code, "severity": f.severity, "message": f.message}
                  for f in findings],
        "clips": clips,
    }


def render_script_txt(script: dict) -> str:
    lines: list[str] = []
    lines.append("DOC-TO-VIDEO-TUTOR | EXACT TTS SCRIPT (verbatim edge-tts input)")
    lines.append("=" * 78)
    lines.append(f"voice profile : {script.get('voice_profile')}")
    lines.append(f"tts voice     : {script.get('tts_voice')}")
    lines.append(f"rate/pitch/vol: {script.get('rate')} / {script.get('pitch')} / "
                 f"{script.get('volume')}")
    lines.append("")
    lines.append("The 'opening' is NOT spoken (it sits silently on the title card).")
    lines.append("Each clip below is the EXACT string sent to edge-tts, in order.")
    lines.append("=" * 78)
    for clip in script.get("clips", []):
        if clip["role"] == "final":
            lines.append("")
            lines.append("--- FINAL TAKEAWAYS CLIP (spoken last, over keys slide) ---")
        else:
            lines.append("")
            lines.append(f"--- SCENE {clip.get('index', '?')} "
                         f"[{clip.get('section', '')}] ({clip.get('word_count', 0)} words) ---")
        lines.append(f"SPOKEN: {clip['spoken']}")
    lines.append("")
    lines.append("=" * 78)
    audit = script.get("audit") or []
    lines.append(f"TTS AUDIT: {len(audit)} finding(s)")
    for f in audit:
        lines.append(f"  [{f['severity']}] {f['code']}: {f['message']}")
    if not audit:
        lines.append("  (clean)")
    return "\n".join(lines)
