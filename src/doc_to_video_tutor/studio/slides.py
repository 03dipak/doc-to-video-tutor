"""Slide raster rendering (PIL) of scenes into PNG frames."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from .config import (
    ACCENT,
    BG,
    BRAND_FOOTER,
    FG,
    GOLD,
    GREEN,
    MUTED,
    PANEL,
    RED,
)
from .util import _Progress


def _wrap(text: str, width: int = 60) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        probe = f"{current} {w}".strip()
        if len(probe) > width:
            if current:
                lines.append(current)
            current = w
        else:
            current = probe
    if current:
        lines.append(current)
    return lines
def _with_overflow(items: list, cap: int = 5) -> list[str]:
    """Keep the visible item count bounded without emitting pipeline metadata."""
    return [str(i) for i in items[:cap]]


def _json_payload_candidates(scene: dict) -> list[list[str]]:
    """Payload line variants, most readable first.

    A scene that already carries a command panel has little vertical budget left,
    so the payload degrades to a single compact line rather than being dropped:
    the concrete data is the point of the block, not its line breaks. Both
    renderers walk this same list and take the first variant that fits, which
    keeps the video frame and the deck in agreement.
    """
    snippet = str(scene.get("json_snippet", "")).strip()
    if not snippet:
        return []
    pretty = snippet.splitlines()[:6]
    body = [line.strip().rstrip(",") for line in pretty
            if line.strip() not in ("{", "}")]
    compact = ["{ " + ", ".join(body) + " }"] if body else []
    return [pretty, compact] if compact and compact != pretty else [pretty]
_CODE_LANGUAGE = {
    "py": "python", "python": "python",
    "sh": "bash", "bash": "bash", "zsh": "bash",
    "yml": "yaml", "yaml": "yaml", "json": "json",
    "js": "javascript", "ts": "typescript",
}


_CODE_TOKEN_COLORS = {
    "Keyword": (255, 193, 7),
    "Keyword.Constant": (255, 121, 198),
    "Name.Function": (66, 133, 244),
    "Name.Class": (118, 214, 224),
    "Name.Builtin": (140, 200, 255),
    "Name.Decorator": (200, 160, 255),
    "Literal.String": (152, 224, 152),
    "Literal.Number": (255, 170, 90),
    "Comment": (128, 140, 160),
    "Operator": (235, 238, 245),
    "Punctuation": (200, 208, 220),
    "Error": (234, 67, 53),
}


def _token_color(token_type: str) -> tuple:
    parts = str(token_type).split(".")
    root = parts[0]
    if root == "Token" or str(token_type).startswith("Token"):
        for index in range(len(parts), 0, -1):
            key = ".".join(parts[1:index]) if index > 1 else parts[-1]
            if key in _CODE_TOKEN_COLORS:
                return _CODE_TOKEN_COLORS[key]
    return (140, 200, 255)


def _code_line_colors(lines: list[str], filename_hint: str = "") -> list:
    """Syntax colours for a code panel, degrading to the flat code colour.

    Pygments only ever recolours text that is already there, so this cannot
    introduce content. If the lexer is unavailable, or the snippet is not
    recognisable, every line falls back to the single code colour the panel used
    before, so rendering never depends on the dependency.
    """
    flat = (140, 200, 255)
    try:
        from pygments import lex
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return [[(line, flat)] for line in lines]
    # The hint is a filename: take the extension, not the stem ("compare.py"
    # -> "py"), otherwise every lookup misses and falls back to flat.
    extension = filename_hint.strip().lower().rsplit(".", 1)[-1].lstrip(".")
    language = _CODE_LANGUAGE.get(extension)
    lexer = None
    for candidate in (language, "text"):
        if not candidate:
            continue
        try:
            lexer = get_lexer_by_name(candidate)
            break
        except ClassNotFound:
            continue
    if lexer is None:
        return [[(line, flat)] for line in lines]
    out: list[list[tuple[str, tuple]]] = []
    for line in lines:
        pieces: list[tuple[str, tuple]] = []
        for token_type, value in lex(line, lexer):
            text = str(value)
            if not text:
                continue
            # Whitespace tokens are kept: dropping them would run words
            # together on the slide ("defcompare") because each piece is
            # positioned by its own measured width.
            pieces.append((text, _token_color(str(token_type))))
        if pieces and pieces[-1][0].endswith("\n"):
            # The lexer appends a newline the source line did not have; keeping
            # it would render a stray line break in the panel.
            pieces[-1] = (pieces[-1][0].rstrip("\n"), pieces[-1][1])
        out.append(pieces or [(line, flat)])
    return out


# Layout findings for the video path, printed by `render_scenes` as
# `[WARN] layout: ...` exactly as the PPTX builder does. The PPTX deck has had
# `_audit_layout` since 43edaa4; this path had no equivalent at all, which is
# how a diagram ran 58px off the right edge of the shipped MP4 for a whole scene
# while `_audit_layout` reported the deck clean. A defect only one renderer can
# see needs a check in that renderer.
LAYOUT_NOTES: list[str] = []

# Diagram row geometry. The per-node cap ignored the gap between nodes, so the
# row outgrew the frame: at 6 nodes `w = min((1280-120)//6, 230) = 193` with
# `pitch = w + 24 = 217` puts the last node's right edge at
# `60 + 5*217 + 193 = 1338px` on a 1280px frame - 58px, about 30% of the pill,
# past the edge. The cap has to be applied to the whole row, gaps included.
_DIAGRAM_NODE_CAP = 230
_DIAGRAM_MIN_NODE = 60


def _diagram_row_fit(count: int, W: int = 1280, fx: int = 60,
                     gap: int = 24, cap: int = _DIAGRAM_NODE_CAP
                     ) -> tuple[int, int, int]:
    """(node_width, pitch, right_edge) for `count` diagram nodes.

    Fits the row inside the content band `fx .. W - fx` including the `count-1`
    inter-node gaps, so no node can leave the frame. Sizes of 4 or fewer come
    out unchanged from the old per-node cap, so the common case renders
    identically and this is not a visual regression.
    """
    budget = W - 2 * fx
    usable = budget - (count - 1) * gap
    w = max(_DIAGRAM_MIN_NODE, min(cap, usable // count)) if count else cap
    pitch = w + gap
    right = fx + (count - 1) * pitch + w
    return w, pitch, right


def diagram_overflow_px(count: int, W: int = 1280, fx: int = 60,
                        gap: int = 24) -> int:
    """Pixels of the last node lost past the FRAME edge under the OLD cap.

    Kept deliberately: it reproduces the shipped defect's arithmetic so a
    regression test can prove the bug is still detectable, rather than only
    proving the new sizing is quiet. Measured against the frame, not the
    content band - 6 nodes put the node's right edge at 1338px on a 1280px
    frame, and 58px of the pill is what the viewer actually loses. The new fit
    targets the stricter content band (`fx .. W - fx`), so it clears both.
    """
    w = min((W - 120) // count, _DIAGRAM_NODE_CAP) if count else 0
    right = fx + (count - 1) * (w + gap) + w
    return max(0, right - W)


def _rounded_rect(draw, box, radius=18, fill=PANEL):
    draw.rounded_rectangle(box, radius=radius, fill=fill)
def _parse_diagram(text: str) -> list[str] | None:
    """Parse a '[...] ---> [...] ---> ...' chain into node labels."""
    nodes: list[str] = []
    for part in re.split(r"\s*(?:<?-{2,}>|->|=>|→)\s*", text or ""):
        labels = [x.strip() for x in (re.findall(r"\[([^\]]+)\]", part) or [part])]
        for label in labels:
            if label:
                nodes.append(label)
    return nodes or None
_TTF_REGULAR = next((p for p in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
) if os.path.exists(p)), None)
_TTF_BOLD = next((p for p in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
) if os.path.exists(p)), None)
_TTF_MONO = next((p for p in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
) if os.path.exists(p)), None)
_font_cache: dict[tuple[int, bool, bool], object] = {}
def _font(size: int, bold: bool = False, mono: bool = False):
    """Cached truetype font (or None -> PIL default bitmap) with TTF fallback."""
    from PIL import ImageFont
    key = (size, bold, mono)
    if key in _font_cache:
        return _font_cache[key]
    path = _TTF_MONO if mono else (_TTF_BOLD if bold else _TTF_REGULAR)
    font: object = None
    if path is not None:
        try:
            font = ImageFont.truetype(path, int(size))
        except Exception:
            font = None
    _font_cache[key] = font
    return font
def render_slide(scene: dict, index: int, total: int, out_path: Path,
                 reveal_upto: int | None = None, highlight: int | None = None) -> None:
    from PIL import Image, ImageDraw

    # reveal_upto: draw only bullets[:reveal_upto] (space after is reserved so
    # every variant shares one layout). highlight: index of the bullet drawn as
    # the 'current' one (accent ring + pointer bar), as a teacher would indicate.
    S = 1.0  # 1280x720 native (S=1.5 would render 1080p)
    W, H = 1280 * S, 720 * S
    img = Image.new("RGB", (int(W), int(H)), color=BG)
    d = ImageDraw.Draw(img)

    def x(v):
        return int(v * S)

    def y(v):
        return int(v * S)

    BODY_MAX = int(H) - 68

    def room(need: int) -> bool:
        return yy + need < BODY_MAX

    section_ft = _font(15, bold=True)
    title_ft = _font(30, bold=True)
    counter_ft = _font(14)
    label_ft = _font(14, bold=True)
    body_ft = _font(14)
    step_num_ft = _font(13, bold=True)
    flow_ft = _font(14, bold=True)
    code_ft = _font(15, mono=True)
    foot_ft = _font(13)

    # header
    d.rectangle([0, 0, int(W), 15], fill=ACCENT)
    d.text((x(40), y(30)), scene.get("section", "").upper(), fill=GOLD, font=section_ft)
    d.text((x(40), y(68)), scene.get("title", "")[:70], fill=(255, 255, 255), font=title_ft)
    d.line([x(40), y(124), int(W) - x(40), y(124)], fill=GOLD, width=3)
    d.text((int(W) - x(220), y(32)), f"{index + 1} / {total}", fill=MUTED, font=counter_ft)

    yy = 150

    # design decision card (interview framing: X not Y because Z) - always the
    # first thing read, so it sits on top of the slide under the divider.
    decision = str(scene.get("design_decision", "")).strip()
    if decision:
        dlines = _wrap(decision, 84)[:2]
        dbox = max(58, 44 + len(dlines) * 24)
        yy += 2
        _rounded_rect(d, [x(50), y(yy), int(W) - x(50), y(yy + dbox)],
                      radius=22, fill=PANEL)
        d.rectangle([x(50), y(yy), x(57), y(yy + dbox)], fill=ACCENT)
        d.text((x(72), y(yy + 10)), "WHY THIS", fill=GOLD, font=label_ft)
        for j, line in enumerate(dlines):
            d.text((x(72), y(yy + 32 + j * 24)), line, fill=FG, font=body_ft)
        yy += dbox + 12

    # numbered steps
    steps = scene.get("steps") or []
    if steps:
        d.text((x(50), y(yy)), "STEPS", fill=GREEN, font=label_ft)
        yy += 32
        for i, s in enumerate(steps, 1):
            if not room(76):
                break
            d.ellipse([x(55), y(yy), x(85), y(yy + 30)], fill=GREEN)
            d.text((x(60), y(yy + 7)), str(i), fill=BG, font=step_num_ft)
            for j, line in enumerate(_wrap(s, 60)[:2]):
                d.text((x(100), y(yy + j * 24)), line, fill=FG, font=body_ft)
            yy += 72
        yy += 10

    # bullets (progressive reveal: hidden bullets keep their slot so the
    # layout is identical across every reveal variant).
    bullets = scene.get("bullets") or []
    if bullets or not scene.get("takeaways"):
        d.text((x(50), y(yy)), "KEY POINTS", fill=ACCENT, font=label_ft)
        yy += 36
    shown = _with_overflow(bullets)
    vis = len(shown) if reveal_upto is None else min(reveal_upto, len(shown))
    for idx, b in enumerate(shown):
        if not room(54):
            break
        if idx < vis:
            now = highlight == idx
            d.ellipse([x(58), y(yy + 7), x(66), y(yy + 15)],
                      fill=ACCENT if now else GOLD)
            if now:
                d.line([x(44), y(yy + 4), x(44), y(yy + 34)], fill=ACCENT, width=3)
            for j, line in enumerate(_wrap(b, 70)[:2]):
                d.text((x(78), y(yy + j * 24)), line,
                       fill=(255, 255, 255) if now else FG, font=body_ft)
        yy += 50

    # status badges: the exit-code / verdict states, colour-coded so the
    # outcome is readable at a glance instead of being parsed from prose.
    badges = scene.get("status_badges") or []
    if badges and room(58):
        yy += 4
        d.text((x(50), y(yy)), "VERDICT CODES", fill=GOLD, font=label_ft)
        yy += 30
        chip_w = (int(W) - x(100)) // max(len(badges), 1)
        for i, badge in enumerate(badges[:6]):
            state = str(badge.get("state", "")) if isinstance(badge, dict) else str(badge)
            code = str(badge.get("code", "")) if isinstance(badge, dict) else ""
            label = str(badge.get("label", "")) if isinstance(badge, dict) else ""
            fill = {"PASS": GREEN, "FAIL": RED, "REVIEW": GOLD}.get(
                state.upper(), MUTED)
            bx = x(50) + i * chip_w
            _rounded_rect(d, [bx, y(yy), bx + chip_w - x(14), y(yy + 46)],
                          radius=14, fill=fill)
            d.text((bx + x(12), y(yy + 6)), f"{code} {label}".strip()[:18],
                   fill=(18, 24, 38), font=_font(15, bold=True))
        yy += 54

    # flow pipeline
    flow = scene.get("flow") or []
    if flow and room(y(80)):
        w = min((int(W) - x(120)) // len(flow), x(240))
        fx = x(60)
        fy = y(yy + 18)
        for i, node in enumerate(flow):
            xx = fx + i * (w + x(24))
            _rounded_rect(d, [xx, fy, xx + w, fy + y(60)], radius=21, fill=PANEL)
            d.text((xx + x(16), fy + y(22)), node[:26], fill=FG, font=flow_ft)
            if i < len(flow) - 1:
                ax = xx + w
                d.line([ax + 2, fy + y(30), ax + x(18), fy + y(30)], fill=GOLD, width=4)
                d.polygon(
                    [(ax + x(18), fy + y(30)), (ax + x(10), fy + y(24)), (ax + x(10), fy + y(36))],
                    fill=GOLD,
                )
        yy = fy + y(78)

    # visual diagram (whiteboard chain)
    diagram = _parse_diagram(scene.get("visual_diagram") or "")
    if diagram and room(y(120)):
        yy += 8
        d.text((x(50), y(yy)), "DIAGRAM", fill=ACCENT, font=label_ft)
        yy += 38
        fx = x(60)
        # Fit the whole row, gaps included - see `_diagram_row_fit`.
        w, pitch, right_edge = _diagram_row_fit(
            len(diagram), W=int(W), fx=fx, gap=x(24), cap=x(230))
        if right_edge > int(W) - fx:
            # Report the loss past the FRAME, not past the content band. The
            # fit targets the band (stricter), but "58px of the pill is off
            # screen" is the number a viewer would recognise; a band-relative
            # figure overstates it by exactly the margin.
            LAYOUT_NOTES.append(
                f"diagram of {len(diagram)} node(s) puts its last node's right "
                f"edge at {right_edge}px, {right_edge - int(W)}px past the "
                f"{int(W)}px frame, even at the minimum node width; that node "
                f"is unreadable")
        fy = y(yy)
        for i, node in enumerate(diagram):
            xx = fx + i * pitch
            _rounded_rect(d, [xx, fy, xx + w, fy + y(56)], radius=20, fill=PANEL)
            for j, line in enumerate(_wrap(node, 22)[:2]):
                d.text((xx + x(14), fy + y(16 + j * 20)), line, fill=FG, font=body_ft)
            if i < len(diagram) - 1:
                ax = xx + w
                d.line([ax + 2, fy + y(28), ax + x(18), fy + y(28)], fill=GOLD, width=4)
                d.polygon(
                    [(ax + x(18), fy + y(28)),
                     (ax + x(10), fy + y(22)),
                     (ax + x(10), fy + y(34))],
                    fill=GOLD,
                )
        yy = fy + y(84)

    # analogy card
    analogy = scene.get("analogy")
    if analogy and room(70):
        yy += 4
        _rounded_rect(d, [x(50), y(yy), int(W) - x(50), y(yy + 62)], radius=24, fill=(40, 30, 20))
        d.text((x(66), y(yy + 10)), "ANALOGY", fill=GOLD, font=label_ft)
        for j, line in enumerate(_wrap(analogy, 80)[:2]):
            d.text((x(66), y(yy + 32 + j * 22)), line, fill=FG, font=body_ft)
        yy += 62

    # code snippet panel
    code_text = scene.get("code_snippet")
    if code_text:
        code_lines = str(code_text).splitlines()[:8]
        code_context = str(scene.get("code_context", "")).strip()
        context_lines = _wrap(code_context, 78)[:1] if code_context else []
        code_start = 32 + (20 if context_lines else 0)
        box_h = 16 + len(context_lines) * 20 + len(code_lines) * 20 + 10
        if room(box_h + 8):
            yy += 6
            _rounded_rect(d, [x(50), y(yy), int(W) - x(50), y(yy + box_h)],
                          radius=18, fill=(10, 16, 26))
            d.text((x(66), y(yy + 8)),
                   "COMMAND CONTEXT" if context_lines else "CODE",
                   fill=GOLD, font=label_ft)
            if context_lines:
                d.text((x(72), y(yy + 30)), context_lines[0], fill=FG, font=body_ft)
            hint = str(scene.get("code_language", "")).strip()
            for j, parts in enumerate(_code_line_colors(code_lines, hint)):
                cx = x(70)
                for token, colour in parts:
                    d.text((cx, y(yy + code_start + j * 20)), token[:64],
                           fill=colour, font=code_ft)
                    cx += int(d.textlength(token[:64], font=code_ft))
            yy += box_h + 12

    # value table: the concrete boundary numbers behind an abstract rule
    value_table = scene.get("value_table") or []
    if value_table and room(40 + 30 * len(value_table)):
        yy += 6
        _rounded_rect(d, [x(50), y(yy), int(W) - x(50), y(yy + 40 + 30 * len(value_table))],
                      radius=18, fill=(26, 34, 52))
        d.text((x(66), y(yy + 10)), "NUMBERS", fill=GOLD, font=label_ft)
        for j, row in enumerate(value_table[:4]):
            left, _, right = str(row).partition("|")
            row_y = yy + 40 + j * 30
            d.text((x(72), y(row_y)), left.strip()[:34], fill=FG, font=body_ft)
            d.text((int(W) - x(330), y(row_y)), right.strip()[:34],
                   fill=(140, 200, 255), font=code_ft)
        yy += 40 + 30 * len(value_table[:4]) + 12

    # json payload: the literal shape of a file the slide talks about
    for json_lines in _json_payload_candidates(scene):
        box_h = 16 + len(json_lines) * 20 + 10
        if not room(box_h + 8):
            continue
        yy += 6
        _rounded_rect(d, [x(50), y(yy), int(W) - x(50), y(yy + box_h)],
                      radius=18, fill=(14, 22, 34))
        d.text((x(66), y(yy + 8)), "PAYLOAD", fill=GOLD, font=label_ft)
        for j, ln in enumerate(json_lines):
            d.text((x(72), y(yy + 32 + j * 20)), ln[:118],
                   fill=(180, 220, 255), font=code_ft)
        yy += box_h + 12
        break

    # takeaway when present
    takeaways = scene.get("takeaways") or []
    if takeaways:
        yy += 10
        d.text((x(50), y(yy)), "TAKEAWAY", fill=GREEN, font=label_ft)
        yy += 32
        shown_t = _with_overflow(takeaways)
        vis_t = len(shown_t) if reveal_upto is None else min(reveal_upto, len(shown_t))
        # Two balanced columns. Every item still advances the vertical budget -
        # `room()` gates the block, `tops` only decides which column an item
        # lands in - so the existing overflow guard keeps its meaning and the
        # reveal order still reads down-then-across.
        tops = [yy, yy]
        for idx, t in enumerate(shown_t):
            col = _shorter_column(tops)
            row_y = tops[col]
            if not room(44):
                break
            if idx < vis_t:
                now = highlight == idx
                cx = _TAKEAWAY_COL_X[col]
                d.ellipse([x(cx + 14), y(row_y + 6), x(cx + 22), y(row_y + 14)],
                          fill=ACCENT if now else GOLD)
                if now:
                    d.line([x(cx), y(row_y + 4), x(cx), y(row_y + 32)],
                           fill=ACCENT, width=3)
                for j, line in enumerate(_wrap(t, 52)[:2]):
                    d.text((x(cx + 34), y(row_y + j * 24)), line,
                           fill=(255, 255, 255) if now else FG, font=body_ft)
            tops[col] = row_y + _TAKEAWAY_ROW_H
        # The budget must advance past the deepest column, or the next block
        # would be laid out on top of the shorter one.
        yy = max(tops)

    d.text((x(40), int(H) - y(44)), BRAND_FOOTER,
            fill=MUTED, font=foot_ft)
    img.save(out_path)
def _render_title_card(plan: dict, out_path: Path) -> None:

    """Cover card with the same header chrome as scene slides (no counter)."""
    from PIL import Image, ImageDraw

    W, H = 1280, 720
    img = Image.new("RGB", (W, H), color=BG)
    d = ImageDraw.Draw(img)

    section_ft = _font(15, bold=True)
    title_ft = _font(30, bold=True)
    opening_ft = _font(17)
    foot_ft = _font(13)

    d.rectangle([0, 0, W, 15], fill=ACCENT)
    d.text((40, 30), "LESSON", fill=GOLD, font=section_ft)
    d.text((40, 68), str(plan.get("title", "Lesson"))[:70],
           fill=(255, 255, 255), font=title_ft)
    d.line([40, 124, W - 40, 124], fill=GOLD, width=3)

    yy = 175
    opening = str(plan.get("opening", "")).strip()
    if opening:
        for line in _wrap(opening, 64)[:10]:
            d.text((40, yy), line, fill=MUTED, font=opening_ft)
            yy += 31

    d.text((40, H - 44), BRAND_FOOTER,
            fill=MUTED, font=foot_ft)
    img.save(out_path)
# Two-column geometry for the takeaway page, shared with the PPTX builder.
# The video path had none: every takeaway was left-packed into x 0.44-5.90in and
# 7.1in of the content width sat empty, on a page that also needed two slides
# for six short lines. The PPTX builder had exactly this defect and fixed it in
# `_shorter_column`; the fix never reached here, which is the §N3 divergence in
# miniature. One helper, both renderers.
TAKEAWAY_COLS = 2
_TAKEAWAY_COL_X = (44, 655)
_TAKEAWAY_COL_W = 575
_TAKEAWAY_ROW_H = 40
# How many items fit down one column, from the takeaway block's start to
# BODY_MAX. 12 is conservative: the block opens at yy ~ 1.7in-equivalent and
# BODY_MAX is H-68, so 12 rows of 40px clears it with margin.
TAKEAWAY_ROWS_PER_COL = 12


def _shorter_column(tops: Sequence[float]) -> int:
    """Index of the column that has consumed less height.

    The original test was "does column 0 still have room below it?", which stays
    true until column 0 is completely full - so the second column was never
    used at all. Verified on three decks: every takeaway sat at x=0.42 and the
    column at x=6.62 was empty. Placing into whichever column is currently
    shorter balances the pair from the first card.
    """
    return 0 if tops[0] <= tops[1] else 1


def _takeaway_pages_by_count(count: int,
                        rows_per_col: int = TAKEAWAY_ROWS_PER_COL,
                    cols: int = TAKEAWAY_COLS) -> int:
    """How many pages `count` takeaways need across `cols` balanced columns.

    Named `_by_count` because `pptx._takeaway_pages_measured` answers the same
    question the other way: it simulates the fill against a real text-height
    estimate, where this is arithmetic. Same decision, two renderers, two
    algorithms - so the names must not invite someone to treat them as one.
    The video's version is an upper bound guarded at draw time by `room()`; the
    deck's is exact because it measures.
    """
    per_page = max(1, rows_per_col * cols)
    return max(1, -(-count // per_page)) if count else 0


def _takeaway_page_items(items: list[str], page: int,
                         rows_per_col: int = TAKEAWAY_ROWS_PER_COL,
                         cols: int = TAKEAWAY_COLS) -> list[str]:
    """The slice of `items` belonging to 1-indexed `page`, in fill order."""
    per_page = max(1, rows_per_col * cols)
    return items[(page - 1) * per_page: page * per_page]


_CONT_MARKER = re.compile(r"\(cont\.\s*\d+\)\s*$")


def mark_continuations(pages: list[dict]) -> list[dict]:
    """Label pages 2..n of one scene `Title (cont. N)`.

    A scene whose bullets paginate produced two or three pages that all carried
    the *identical* title, so a viewer had no way to tell a continuation from a
    repeat - and each page is a separate audio segment, so the mismatch is
    audible too. The takeaway page has emitted `(cont. N)` since its pagination
    landed; scene bodies never did. That asymmetry is the one
    `graphic-reviewer` is asked to look for, and it was real.

    Applied to the FINAL page list rather than inside `_scene_pages`, because the
    two renderers split differently: the deck also splits by measured height, so
    it can produce more pages than the count-based split, and a marker applied
    before that would number the wrong pages.

    Pages are grouped by `_page_group`, stamped by `_scene_pages` and preserved
    through `_paginate_by_height`'s clone, so numbering restarts per scene
    instead of running across the deck.
    """
    by_group: dict[object, list[dict]] = {}
    for page in pages:
        by_group.setdefault(page.get("_page_group"), []).append(page)
    for group in by_group.values():
        if len(group) < 2:
            continue
        for n, page in enumerate(group, 1):
            if n == 1:
                continue
            title = str(page.get("title") or "")
            if not title or _CONT_MARKER.search(title):
                continue
            page["title"] = f"{title} (cont. {n})"
    return pages


def _scene_pages(scene: dict) -> list[dict]:

    raw_bullets = [str(value) for value in (scene.get("bullets") or [])]
    if raw_bullets:
        pages = scene.get("bullet_pages")
        if isinstance(pages, list) and pages:
            candidate_pages = [page for page in pages
                               if isinstance(page, list) and page]
            if (all(len(page) <= 4 for page in candidate_pages)
                    and [str(value) for page in candidate_pages for value in page]
                    == raw_bullets):
                body_pages = [[str(value) for value in page]
                              for page in candidate_pages]
            else:
                body_pages = [raw_bullets[i:i + 4]
                              for i in range(0, len(raw_bullets), 4)]
        else:
            body_pages = [raw_bullets[i:i + 4]
                          for i in range(0, len(raw_bullets), 4)]
        out: list[dict] = []
        for page in body_pages:
            page_scene = dict(scene)
            page_scene["bullets"] = page
            page_scene["_page_group"] = id(scene)
            out.append(page_scene)
        return mark_continuations(out)
    takeaways = [str(value) for value in (scene.get("takeaways") or [])]
    if takeaways:
        pages = _takeaway_pages_by_count(len(takeaways))
        out = []
        for page_no in range(1, pages + 1):
            chunk = _takeaway_page_items(takeaways, page_no)
            if not chunk:
                break
            # The PPTX builder has emitted "Key Takeaways (cont. N)" since its
            # pagination landed; the video path never did, so its second page
            # was titled identically to its first. Same defect, same fix.
            title = str(scene.get("title") or "Key Takeaways")
            if page_no > 1 and not re.search(r"\(cont\.\s*\d+\)$", title):
                title = f"{title} (cont. {page_no})"
            out.append({**scene, "title": title, "takeaways": chunk})
        return out
    return [dict(scene)]


def _slide_variants(scene: dict, index: int, total: int, work_dir: Path) -> list[Path]:
    """Render all paginated reveal variants for one audio scene."""
    body = scene.get("bullets") or scene.get("takeaways") or []
    count = len(body)
    paths: list[Path] = []
    for k in range(count + 1):
        out = work_dir / f"slide_{index:02d}_{k:02d}.png"
        render_slide(scene, index, total, out,
                     reveal_upto=k if count else None,
                     highlight=k - 1 if k else None)
        paths.append(out)
    return paths


def render_scenes(plan: dict, work_dir: Path) -> list[list[Path]]:
    """Render one audio-aligned group per scene, including bullet pagination."""
    scene_pages = [_scene_pages(scene) for scene in plan["scenes"]]
    takeaways = (plan.get("takeaways") or [])[:6]
    if takeaways:
        final_scene = {
            "section": "Key Takeaways",
            "title": "Key Takeaways",
            "bullets": [],
            "steps": [],
            "flow": [],
            "analogy": "",
            "takeaways": takeaways,
        }
        scene_pages.append(_scene_pages(final_scene))
    page_total = sum(len(pages) for pages in scene_pages)
    total = page_total + 1
    page_index = 0
    groups: list[list[Path]] = []
    del LAYOUT_NOTES[:]
    p = _Progress("slides", len(scene_pages))
    for pages in scene_pages:
        group: list[Path] = []
        for page in pages:
            page_index += 1
            group.extend(_slide_variants(page, page_index, total, work_dir))
        groups.append(group)
        p._tick(len(groups))
    p.done()
    # Same channel and wording as the PPTX builder's `[WARN] layout:` notes, so
    # a video-path defect is visible in the build log instead of only in the
    # pixels. Deduped: pagination re-renders one scene several times and would
    # otherwise repeat one note per variant.
    for note in dict.fromkeys(LAYOUT_NOTES):
        print(f"  [WARN] layout: {note}")
    return groups
