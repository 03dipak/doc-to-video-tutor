"""Narration voice policies (MHE/English) and CleanupRule/NarrationVoice dataclasses."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from dataclasses import field as dc_field

from .config import (
    _CLOSER_POOL,
    _DD_LEADS,
    _MHE_MARKERS,
    _NARR_HEADS,
    _OPENER_POOL,
    _POINTS_LEADS,
    _TEMPLATE_AHEM,
    _TEMPLATE_SEEKHTE,
)


@dataclass(frozen=True)
class CleanupRule:
    """Named narration cleanup rule (template/stall collapse) with diagnostics."""

    name: str
    pattern: re.Pattern
@dataclass(frozen=True)
class PronunciationRule:
    """Written->spoken expansion for the TTS layer (word-boundary aware)."""

    written: str
    spoken: str
    word_boundary: bool = True


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six",
                 "seven", "eight", "nine", "ten", "eleven", "twelve")


def _module_rules() -> tuple[PronunciationRule, ...]:
    """M1..M12 -> "module one".."module twelve".

    Generated from a range rather than listed by hand: a hand-written list had
    stopped at m6, so a nine-scene lesson spoke "M7", "M8", "M9" raw and the
    unknown-token audit could not catch them because they are not capitalised
    words. Word-boundary matching keeps "m1" from firing inside a token such as
    "rasm1".
    """
    return tuple(
        PronunciationRule(f"m{index}", f"module {_NUMBER_WORDS[index]}")
        for index in range(1, 13)
    )


_MHE_TECH_PRONUNCIATION: tuple[PronunciationRule, ...] = (
    PronunciationRule("1/(n+1)", "one over n plus one", word_boundary=False),
    *_module_rules(),
    PronunciationRule("schema_version", "schema version"),
    PronunciationRule("baseline_id", "baseline id"),
    PronunciationRule("run_suite", "run suite"),
    PronunciationRule("llm_eval_gate", "l l m eval gate"),
    PronunciationRule("live_eval_nightly", "live eval nightly"),
    PronunciationRule("active.jso", "active dot json", word_boundary=False),
    PronunciationRule("active.json", "active dot json", word_boundary=False),
    PronunciationRule("plan.json", "plan dot json", word_boundary=False),
    PronunciationRule("deep_arch", "deep architectural evaluation", word_boundary=False),
    PronunciationRule("deep architecture", "deep architectural evaluation", word_boundary=False),
    PronunciationRule(".json", " dot json", word_boundary=False),
    PronunciationRule(".yml", " dot yml", word_boundary=False),
    PronunciationRule(".py", " dot py", word_boundary=False),
    PronunciationRule("llm_eval_gate.yml", "l l m eval gate", word_boundary=False),
    PronunciationRule("=", " equals ", word_boundary=False),
    PronunciationRule("&", " and ", word_boundary=False),
    PronunciationRule("+", " plus ", word_boundary=False),
    PronunciationRule("\u2192", " to ", word_boundary=False),
    PronunciationRule("\u2190", " from ", word_boundary=False),
    PronunciationRule(";", ", ", word_boundary=False),
    PronunciationRule("0", " zero "),
    PronunciationRule("1", " one "),
    PronunciationRule("2", " two "),
    PronunciationRule("3", " three "),
    PronunciationRule("4", " four "),
    PronunciationRule("5", " five "),
    PronunciationRule("6", " six "),
    PronunciationRule("7", " seven "),
    PronunciationRule("8", " eight "),
    PronunciationRule("9", " nine "),
)
_ENGLISH_TECH_PRONUNCIATION: tuple[PronunciationRule, ...] = (
    PronunciationRule("1/(n+1)", "one over n plus one", word_boundary=False),
    *_module_rules(),
    PronunciationRule("schema_version", "schema version"),
    PronunciationRule("baseline_id", "baseline id"),
    PronunciationRule("run_suite", "run suite"),
    PronunciationRule("llm_eval_gate", "l l m eval gate"),
    PronunciationRule("live_eval_nightly", "live eval nightly"),
    PronunciationRule("active.jso", "active dot json", word_boundary=False),
    PronunciationRule("active.json", "active dot json", word_boundary=False),
    PronunciationRule("plan.json", "plan dot json", word_boundary=False),
    PronunciationRule("deep_arch", "deep architectural evaluation", word_boundary=False),
    PronunciationRule("deep architecture", "deep architectural evaluation", word_boundary=False),
    PronunciationRule(".json", " dot json", word_boundary=False),
    PronunciationRule(".yml", " dot yaml", word_boundary=False),
    PronunciationRule(".py", " dot py", word_boundary=False),
    PronunciationRule("=", " equals ", word_boundary=False),
    PronunciationRule("&", " and ", word_boundary=False),
    PronunciationRule("+", " plus ", word_boundary=False),
    PronunciationRule("\u2192", " to ", word_boundary=False),
    PronunciationRule("\u2190", " from ", word_boundary=False),
    PronunciationRule(";", ", ", word_boundary=False),
    PronunciationRule("0", " zero "),
    PronunciationRule("1", " one "),
    PronunciationRule("2", " two "),
    PronunciationRule("3", " three "),
    PronunciationRule("4", " four "),
    PronunciationRule("5", " five "),
    PronunciationRule("6", " six "),
    PronunciationRule("7", " seven "),
    PronunciationRule("8", " eight "),
    PronunciationRule("9", " nine "),
)
_MHE_LEAK_PHRASES: tuple[str, ...] = (
    "slide ke", "slide pe", "points likhe hain", "exact points",
    "points pe dhyaan", "asli points", "key points list", "points abhi padhte hain",
    "aur in points", "points pe dhyaan do", "dhyaan do", "aur in do",
    "points, ek ek karke", "ek ek karke", "to ab samajh aata hai",
    "to iska matlab kya hua", "ye kya hai, samjho isse",
    "main points", "yeh key points hain", "key points",
)
_EN_LEAK_PHRASES: tuple[str, ...] = (
    "the slide points", "the bullets are", "these points matter", "the slide says",
    "key points", "the main points", "the slide lists", "points to note",
)
@dataclass(frozen=True)
class NarrationLanguagePolicy:
    """What the QA layer expects of narration for a voice (Layer B policy).

    Split from NarrationVoice so a voice can describe HOW to write while the
    policy describes WHAT to check: two voices may share a language policy
    (e.g. 'english-neutral' vs 'english-concise').
    """

    expected_language: str = "mixed"
    must_not_be_pure_english: bool = False
    required_markers: frozenset[str] = frozenset()
    min_marker_hits: int = 3
    cleanup_rules: tuple[CleanupRule, ...] = ()
@dataclass(frozen=True)
class NarrationVoice:
    """The spoken layer the studio WRITES (openers, closers, leads, labels).

    Layer B of the narration design: every word the engine emits into
    narrations lives here, so switching narration language is a data change,
    not a code change. The repeat-safety machinery (Layer A) is token-only and
    voice-agnostic; a wrong voice can only make narration sound off, never
    break the repeat gate.
    """

    name: str
    openers: tuple[str, ...]
    closers: tuple[str, ...]
    dd_leads: tuple[str, ...]
    points_leads: tuple[str, ...]
    narr_heads: tuple[str, ...]
    takeaways_lead: str = " Key takeaways, yaad rakhein: "
    takeaways_any_lead: str = " Sari takeaways yaad rakhein: "
    takeaways_marker: str = "yaad rakhein"
    bullet_leads: tuple[str, ...] = ()
    tts_voice: str | None = None
    rate: str = "-8%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    pronunciation_rules: tuple[PronunciationRule, ...] = ()
    spoken_meta_leaks: tuple[str, ...] = ()
    policy: NarrationLanguagePolicy = dc_field(
        default_factory=lambda: NarrationLanguagePolicy(
            must_not_be_pure_english=True,
            required_markers=frozenset(_MHE_MARKERS)),
    )

    @property
    def skeletons(self) -> tuple[re.Pattern, ...]:
        return tuple(rule.pattern for rule in self.policy.cleanup_rules)

    @property
    def skeleton_names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self.policy.cleanup_rules)
_MHE_POLICY = NarrationLanguagePolicy(
    expected_language="mixed",
    must_not_be_pure_english=True,
    required_markers=frozenset(_MHE_MARKERS),
    cleanup_rules=(
        CleanupRule(name="mhe_ahem_skeleton", pattern=_TEMPLATE_AHEM),
        CleanupRule(name="mhe_seekhte_skeleton", pattern=_TEMPLATE_SEEKHTE),
    ),
)
_MHE_VOICE = NarrationVoice(
    name="mhe-mix",
    openers=tuple(_OPENER_POOL),
    closers=tuple(_CLOSER_POOL),
    dd_leads=_DD_LEADS,
    points_leads=_POINTS_LEADS,
    narr_heads=_NARR_HEADS,
    bullet_leads=(
        "Dekha jaaye toh",
        "Baat yeh hai ki",
        "Ek zaroori baat yeh hai",
        "Yahan main point yeh hai",
        "Iska matlab simple hai",
    ),
    tts_voice="hi-IN-SwaraNeural",
    pronunciation_rules=_MHE_TECH_PRONUNCIATION,
    spoken_meta_leaks=_MHE_LEAK_PHRASES,
    policy=_MHE_POLICY,
)
_ENGLISH_POLICY = NarrationLanguagePolicy(
    expected_language="english",
    cleanup_rules=(
        CleanupRule(name="en_understand_skeleton",
                    pattern=re.compile(
                        r"let['\u2019]?\s*understand\s+what\s+(?P<c>[^.]*)\.", re.I)),
        CleanupRule(name="en_explain_skeleton",
                    pattern=re.compile(r"let\s+me\s+explain\s+(?P<c>[^.]*)\.", re.I)),
    ),
)
_ENGLISH_VOICE = NarrationVoice(
    name="english",
    openers=(
        "Let's talk about {m}. ",
        "{m} works like this. ",
        "Next up: {m}. ",
        "{m} — here is what it is. ",
        "{m}. Let's dig in. ",
        "Moving on to {m}. ",
        "Now for {m}. ",
        "{m}: here is the key idea. ",
    ),
    closers=(
        " that is what that really means.",
        " now you get the idea.",
        " that is the core of it.",
        " the next scene shows exactly how to use this.",
    ),
    dd_leads=("Reason: ", "Why: ", "Design choice: ", "The decision: ",
              "Core decision: ", "The real reason: ", "Here's why: ", "Chosen because: "),
    points_leads=(" The slide points: ", " The bullets are: ", " These points matter: ",
                  " The slide says: ", " Key points: ", " The main points: ",
                  " The slide lists: ", " Points to note: "),
    narr_heads=("Notice", "Focus here", "Key idea", "Main point", "Then observe",
                "Here see"),
    bullet_leads=(
        "The important thing to remember is",
        "What this really means is",
        "A practical way to put it is",
        "The takeaway to note here is",
    ),
    takeaways_lead=" Key takeaways: ",
    takeaways_any_lead=" Key takeaways: ",
    takeaways_marker="key takeaways",
    tts_voice="en-IN-NeerjaNeural",
    pronunciation_rules=_ENGLISH_TECH_PRONUNCIATION,
    spoken_meta_leaks=_EN_LEAK_PHRASES,
    policy=_ENGLISH_POLICY,
)
_VOICES: dict[str, NarrationVoice] = {
    "mhe-mix": _MHE_VOICE,
    "english": _ENGLISH_VOICE,
}
def _voice_fingerprint(voice: NarrationVoice) -> str:
    payload = json.dumps({
        "name": voice.name,
        "openers": voice.openers,
        "closers": voice.closers,
        "dd_leads": voice.dd_leads,
        "points_leads": voice.points_leads,
        "narr_heads": voice.narr_heads,
        "bullet_leads": voice.bullet_leads,
        "takeaways_lead": voice.takeaways_lead,
        "takeaways_any_lead": voice.takeaways_any_lead,
        "takeaways_marker": voice.takeaways_marker,
        "tts_voice": voice.tts_voice,
        "rate": voice.rate,
        "pitch": voice.pitch,
        "volume": voice.volume,
        "pronunciation_rules": [(r.written, r.spoken) for r in voice.pronunciation_rules],
        "policy": [r.name for r in voice.policy.cleanup_rules],
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
def _make_voice(name: str | None) -> NarrationVoice:
    """Resolve a --narr-voice name; unknown names fail loudly (exit 2)."""
    if name is None:
        return _MHE_VOICE
    v = _VOICES.get(name.strip().lower())
    if v is None:
        print(f"Unknown narration voice {name!r}. Registered: "
              f"{', '.join(sorted(_VOICES))}", file=sys.stderr)
        raise SystemExit(2)
    return v
def _opener_module_name(sc: dict) -> str:
    """Compact, grounded module label for the opener template.

    The 'Is NN me ...' title shape (Hinglish 'isme') must not bleed into the
    opener text: it renders as 'Is me <topic>' and would repeat the exact
    'hain is me' trigram in every scene opener. Leading 'is'/'me'/numeric
    tokens are skipped so the label is the real topic words only.
    """
    t = str(sc.get("title", "")).split("(")[0].strip()
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", t)]
    while words and (words[0].lower() in ("is", "me") or words[0].isdigit()):
        words.pop(0)
    return " ".join(words[:3]) or "this"
def _assign_openers(plan: dict, voice: NarrationVoice | None = None) -> None:
    """Deterministically force a distinct spoken opener per scene.

    The 7B converges on some attractor opener every run ('aaj hum seekhenge
    ki...'), so no prompt reliably fixes this. Replacing the model's (usually
    filler) first sentence with a rotated, distinct opener guarantees
    variety with zero LLM spend.
    """
    voice = voice or _MHE_VOICE
    scenes = plan.get("scenes", [])
    for i, sc in enumerate(scenes):
        pattern = voice.openers[i % len(voice.openers)]
        prefix = pattern.format(m=_opener_module_name(sc))
        narr = str(sc.get("narration", "")).strip()
        sentences = re.split(r"(?<=[.!?])\s+", narr)
        rest = " ".join(sentences[1:]).strip() if len(sentences) >= 2 else narr
        sc["narration"] = prefix + (rest if rest else narr)
def _assign_closers(plan: dict, every_n: int = 2,
                    voice: NarrationVoice | None = None) -> None:
    """Append rotated rhetorical beats on alternating scenes (call-answer rhythm)."""
    voice = voice or _MHE_VOICE
    used = 0
    for i, sc in enumerate(plan.get("scenes", [])):
        if (i + 1) % every_n == 0:
            closer = voice.closers[used % len(voice.closers)]
            used += 1
            narr = str(sc.get("narration", "")).rstrip()
            if not narr.endswith(("?", "।", ".", "!")):
                narr += "."
            sc["narration"] = narr + closer
