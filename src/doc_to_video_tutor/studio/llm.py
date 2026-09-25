"""LLM HTTP client: ask_llm, bounded content, json parsing, topic extraction."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import requests

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, TOPIC_PROMPT

# Finish reason of the most recent completion ("stop" | "length"). "length"
# means the free 7B hit its output cap mid-object; never trust that response.
_LAST_FINISH: str = "stop"


# P1 conservative character-to-token ratio: 2.5 chars/token (denser than the
# old chars/4 guess because Hinglish/MHE, code blocks, and Devanagari all run
# ~2.0-2.8 chars/token under subword tokenizers), scaled by a 20% headroom
# slack. Reserve = chars / 2.5 * 1.20 = chars * 0.48.
_P1_CHARS_PER_TOKEN = 2.5
_P1_HEADROOM_SLACK = 1.20


def planner_output_budget(prompt_chars: int) -> int:
    """Completion headroom for a prompt of `prompt_chars` characters.

    Free 7B hubs cap the shared context around 8192 tokens. Python's rough
    C tokens/4 guess tracks English, but narration, markdown, code and
    Devanagari tokenize denser (down to ~2-2.8 chars/token), and
    underestimating input is exactly what caused 400s / mid-plan truncation.
    So reserve at 2.5 chars/token plus a fixed 20% slack (0.48 tokens/char).
    `choose_planner_budget` steps the source window down whenever the
    remaining headroom drops below what a plan needs, trading input width for
    guaranteed output budget instead of risking a context cutoff.
    """
    return min(3072, max(512, 8192
                         - int(prompt_chars / _P1_CHARS_PER_TOKEN
                               * _P1_HEADROOM_SLACK)
                         - 480))


# Source windows the planner probes, widest first. The old ladder stopped at
# 8000 chars, but for dense docs (big template + topic/outline/design blocks on
# top of the source) the 8000-char window still left the completion budget
# clamped to its 512-token floor - and a 12-scene plan frame measures ~1.2k-2.2k
# output tokens, so max_tokens=512 cut every plan mid-JSON. The retry path then
# MIS-read that as an input-window problem and kept re-shrinking to 8000 while
# restarting at the same starving budget. Pick the WIDEST window whose projected
# output reservation clears PLANNER_MIN_OUTPUT; step below 8000 as far as that
# takes, trading input width for guaranteed output.
_PLANNER_WINDOWS = (12000, 8000, 6000, 4000, 2400)
# Minimum completion budget a full planner call must receive. The narration
# fields are blank at planning time (Phase-2 B-lite), so the whole output is the
# JSON frame: 5-12 scenes at ~250-350 tokens each + envelope. Reserving below
# ~2048 makes mid-JSON truncation (finish_reason=length / unbalanced JSON)
# inevitable, which is exactly the failure class this fix removes.
PLANNER_MIN_OUTPUT = 2048


def choose_planner_budget(content: str,
                          planner_prompt_for: Callable[[str], str],
                          ) -> tuple[int, int]:
    """Pick (source_window, max_output_tokens) for ONE full-planner call.

    Prefer the widest source window whose projected completion budget clears
    ``PLANNER_MIN_OUTPUT``; if even 12000 chars leaves too little headroom the
    ladder steps the DOCUMENTS down to 8000/6000/4000/2400 to hand back real
    output budget. This keeps prompt+completion inside the hub window AND makes
    an exhausted model the only way a plan can be cut - never the old 512-token
    starvation floor. If even the 2400-char window cannot clear the floor
    (pathologically large prompt), the best achievable budget is returned so
    the 512-token cap is still retired.
    """
    best_budget = 512
    for window in _PLANNER_WINDOWS:
        budget = planner_output_budget(
            len(planner_prompt_for(_bounded_content(content, window))))
        best_budget = max(best_budget, budget)
        if budget >= PLANNER_MIN_OUTPUT:
            return window, budget
    return _PLANNER_WINDOWS[-1], best_budget


def _looks_truncated(raw: str) -> bool:
    """Heuristic: did the free 7B cut the completion off mid-token?

    A finished fenced-JSON plan (```json ... ```) has balanced
    braces/brackets/parens; a response cut at the output cap is missing its
    closers (positive imbalance). A dangling Unicode replacement char marks a
    corrupted tail. NOTE: a trailing ``` is the normal closing JSON fence and
    is NOT a truncation signal.
    """
    if raw.endswith("\ufffd"):
        return True
    brace = raw.count("{") - raw.count("}")
    bracket = raw.count("[") - raw.count("]")
    paren = raw.count("(") - raw.count(")")
    return max(abs(brace), abs(bracket), abs(paren)) > 1


def _was_truncated(raw: str) -> bool:
    return _LAST_FINISH == "length" or _looks_truncated(raw)


def ask_llm(prompt: str, max_tokens: int | None = None) -> str:
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        raise RuntimeError(
            "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL not set. "
            "Copy .env.example to .env and fill in values."
        )
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    # Free 7B hubs cap the shared context window (e.g. 8192 tokens). Size the
    # output budget from a conservative token estimate so prompt+completion stays
    # inside the window and the server never truncates the plan mid-object.
    output_budget = planner_output_budget(len(prompt))
    body: dict = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": max_tokens or output_budget,
    }

    def _call(payload: dict) -> tuple[str | None, str | None]:
        global _LAST_FINISH
        r = requests.post(url, headers=headers, json=payload, timeout=600)
        r.raise_for_status()
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, json.dumps(data, ensure_ascii=False)[:400]
        if not isinstance(content, str):
            raise RuntimeError(f"LLM returned non-string content: {type(content)!r}")
        try:
            _LAST_FINISH = str(data["choices"][0].get("finish_reason", "stop"))
        except (KeyError, IndexError, TypeError):
            _LAST_FINISH = "stop"
        return content.strip(), None

    # Free 7B endpoints echo template closers (e.g. "ek ahem module hai"); nudge the
    # sampler with repetition/presence penalties where the endpoint supports them.
    content: str | None = None
    err: str | None = None
    try:
        last_exc: requests.HTTPError | None = None
        for attempt_no in range(3):
            try:
                penalties: dict = {"repetition_penalty": 1.05, "presence_penalty": 0.2}
                content, err = _call({**penalties, **body})
                break
            except requests.HTTPError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    # Free hubs throttle bursts; back off and re-send intact.
                    print(f"\n      rate-limited (429); backing off "
                          f"{12 * (attempt_no + 1)}s ... ", end="", flush=True)
                    time.sleep(12 * (attempt_no + 1))
                    continue
                if status in (400, 422):
                    # Endpoint rejected the sampling params (e.g. 400 - unknown
                    # param): retry once with the plain body. Never retry
                    # auth/not-found errors.
                    content, err = _call(body)
                else:
                    raise
                break
            except (requests.ConnectionError, requests.Timeout):
                # Server hiccup on a local vLLM/AWQ box: one retry, then raise.
                content, err = _call(body)
                break
        if content is None and last_exc is not None and last_exc.response is not None \
                and last_exc.response.status_code == 429:
            raise RuntimeError("LLM endpoint rate-limited (429) even after backoff")
        if content is None:
            # 200-with-error payload (params or max_tokens unsupported): retry plain.
            content, _ = _call(body)
        if content is not None:
            return content
        raise RuntimeError(f"LLM endpoint returned an unexpected payload: {err}")
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise RuntimeError(f"LLM endpoint unreachable: {exc}") from exc
def _ask_llm_stable(prompt: str, tries: int = 3,
                    max_tokens: int | None = None) -> str:
    """Sample ask_llm, regenerating when the model degenerates into loops."""
    for attempt in range(tries - 1):
        raw = ask_llm(prompt, max_tokens=max_tokens)
        low = raw.lower()
        words = raw.split()
        if not words:
            return raw
        peak = max(words.count(w) for w in set(words))
        # Degenerate signatures: token repetition loops, or an echoed/nested
        # second ``` JSON fence (the 7B re-emits the schema block inside a
        # narration string, which corrupts the outer JSON).
        first_fence = low.find("```json")
        nested_fence = low.count("```") > 2 or (
            first_fence != -1
            and low.find("```json", first_fence + len("```json")) != -1)
        if (len(words) > 40 and peak / len(words) > 0.35) or nested_fence:
            cause = "nested code fence" if nested_fence else "repetition loop"
            print(f"\n  [1/5] LLM degenerated ({cause}); "
                  f"regenerating ({attempt + 1}/{tries}) ... ", end="", flush=True)
            continue
        return raw
    return ask_llm(prompt, max_tokens=max_tokens)
def _bounded_content(content: str, window: int = 12000) -> str:
    """Trim docs to a token-safe window (head-priority, tail kept).

    The free 7B hub caps input+output around 8192 tokens; a large multi-file
    doc set can push the planner's char/4 token estimate into the clamp and get
    400 (max context exceeded). Keep the leading module tables *and* the last
    sections (where interface/contrast prose tends to live), so the DOCUMENTS
    stay grounded as long as they physically fit.
    """
    if len(content) <= window:
        return content
    head = content[: int(window * 0.7)]
    tail = content[-int(window * 0.3):]
    return (head
            + "\n...[middle sections of the documents truncated to fit context; "
              "keep using only the facts given above and below, never invent]...\n"
            + tail)
def _parse_plan_json(raw: str) -> dict:
    """Extract + validate a JSON lesson plan from a noisy LLM response.

    Scans for the first *complete* JSON object that carries scenes, so
    markdown fences, trailing prose, and ragged junk around the plan are
    tolerated instead of failing the build.
    """
    if _was_truncated(raw):
        raise RuntimeError(
            "LLM plan output truncated (finish_reason=length or unbalanced "
            "JSON); shrinking the source window and retrying")
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = raw.find("{", idx)
        if idx == -1:
            raise RuntimeError("LLM did not return a JSON lesson plan:\n" + raw[:500])
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict) and obj.get("scenes"):
            return obj
        idx = end
def _parse_scene_json(raw: str) -> dict:
    """Extract the first complete JSON object that looks like a single scene."""
    if _was_truncated(raw):
        raise RuntimeError(
            "LLM scene output truncated (finish_reason=length or unbalanced "
            "JSON); keeping the original scene")
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = raw.find("{", idx)
        if idx == -1:
            raise RuntimeError("LLM did not return a scene JSON:\n" + raw[:500])
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict) and obj.get("title") and obj.get("narration"):
            return obj
        idx = end
def _extract_topics(content: str) -> list[str]:
    """Ask the LLM for the 1-3 central topics so the lesson stays on-topic."""
    raw = ask_llm(TOPIC_PROMPT.format(content=_bounded_content(content, 24000)),
                  max_tokens=640)  # topics JSON is tiny; wide doc window OK
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        topics = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [str(t).strip() for t in topics if isinstance(t, str) and t.strip()]
