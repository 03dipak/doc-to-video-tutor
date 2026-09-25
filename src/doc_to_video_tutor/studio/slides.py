"""Slide raster rendering (PIL) of scenes into PNG frames."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import ACCENT, BG, BRAND_FOOTER, FG, GOLD, GREEN, MUTED, PANEL
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
        w = min((int(W) - x(120)) // len(diagram), x(230))
        fx = x(60)
        fy = y(yy)
        for i, node in enumerate(diagram):
            xx = fx + i * (w + x(24))
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
            for j, ln in enumerate(code_lines):
                d.text((x(70), y(yy + code_start + j * 20)), ln[:64],
                       fill=(140, 200, 255), font=code_ft)
            yy += box_h + 12

    # takeaway when present
    takeaways = scene.get("takeaways") or []
    if takeaways:
        yy += 10
        d.text((x(50), y(yy)), "TAKEAWAY", fill=GREEN, font=label_ft)
        yy += 32
        shown_t = _with_overflow(takeaways)
        vis_t = len(shown_t) if reveal_upto is None else min(reveal_upto, len(shown_t))
        for idx, t in enumerate(shown_t):
            if not room(44):
                break
            if idx < vis_t:
                now = highlight == idx
                d.ellipse([x(58), y(yy + 6), x(66), y(yy + 14)],
                          fill=ACCENT if now else GOLD)
                if now:
                    d.line([x(44), y(yy + 4), x(44), y(yy + 32)], fill=ACCENT, width=3)
                for j, line in enumerate(_wrap(t, 70)[:2]):
                    d.text((x(78), y(yy + j * 24)), line,
                           fill=(255, 255, 255) if now else FG, font=body_ft)
            yy += 40

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
            out.append(page_scene)
        return out
    takeaways = [str(value) for value in (scene.get("takeaways") or [])]
    if takeaways:
        return [
            {**scene, "takeaways": takeaways[i:i + 4]}
            for i in range(0, len(takeaways), 4)
        ]
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
    p = _Progress("slides", len(scene_pages))
    for pages in scene_pages:
        group: list[Path] = []
        for page in pages:
            page_index += 1
            group.extend(_slide_variants(page, page_index, total, work_dir))
        groups.append(group)
        p._tick(len(groups))
    p.done()
    return groups
