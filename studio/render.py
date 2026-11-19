"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, load, roman
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


FULL_GAMEPLAY = "full_gameplay"
GAMEPLAY_LAYOUTS = frozenset(("gameplay", FULL_GAMEPLAY))
LAYOUTS = {FULL_GAMEPLAY: "Full Gameplay / TikTok", "blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill",
           "crop": "Fill the screen (centre crop)", "gameplay": "Gameplay + facecam (blurred fill)"}

_TIKTOK_TEXT_X = 490  # reserve more space for controls on the viewer's right
_TIKTOK_CAPTION_Y = 1420
_TIKTOK_GAME_MAX_H = 640
_TIKTOK_FACE_CENTER = 516
_TIKTOK_FACE_SIZE = (448, 216)


def choose_layout(preference, gaming):
    if preference in LAYOUTS:
        return preference
    return FULL_GAMEPLAY if gaming else "blur"


def full_gameplay_region(width, height, sar=1.0, *, facecam=False):
    """Largest square-pixel gameplay rectangle that fits without increasing either source dimension."""
    aspect = width * sar / height
    max_w, max_h = min(W, width) // 2 * 2, min(_TIKTOK_GAME_MAX_H, height) // 2 * 2
    w = max(2, min(max_w, round(max_h * aspect / 2) * 2))
    h = max(2, min(max_h, round(max_w / aspect / 2) * 2))
    x = (W - w) // 4 * 2
    y = ((1008 if facecam else 960) - h // 2) // 2 * 2
    return x, y, w, h


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout == "blur":
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout == "gameplay":
        return layout, fg_h, 235, 1500
    if layout == "crop":
        return layout, fg_h, 300, 1380
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)


def _hook_lines(words, font, size, max_width):
    line = " ".join(words)
    if len(words) < 2 or font.width(line, size) <= max_width:
        return [line]
    cut = min(range(1, len(words)), key=lambda i: max(font.width(" ".join(words[:i]), size),
                                                     font.width(" ".join(words[i:]), size)))
    return [" ".join(words[:cut]), " ".join(words[cut:])]


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    f, size = _Font.get(HOOK["font"]), HOOK["size"]
    words = hook.split()
    if not words:
        return []
    lines = _hook_lines(words, f, size, HOOK["max_w"])
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def _tiktok_hook(hook, dur, y):
    words = hook.split()[:5]
    if not words:
        return []
    font, size, safe_width = _Font.get(HOOK["font"]), 112, 760
    lines = _hook_lines(words, font, size, safe_width)
    widest = max(font.width(line, size) for line in lines)
    if widest > safe_width:
        size = max(68, int(size * safe_width / widest))
    for i, line in enumerate(lines):
        if font.width(line, size) > safe_width:
            while font.width(line + "…", size) > safe_width:
                line = line[:-1]
            lines[i] = line.rstrip() + "…"
    line_h = font.em(size) * 1.15
    return [f"Dialogue: 4,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,"
            f"{{\\an5\\pos({_TIKTOK_TEXT_X},{y + line_h * (i - (len(lines) - 1) / 2) - font.cap_shift(size):.0f})"
            f"\\fs{size}\\1c&HFFFFFF&}}{line}" for i, line in enumerate(lines)]


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, *, gameplay=False, tiktok=False) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    if tiktok:
        st = {**st, "font": HOOK["font"], "size": min(st["size"], 118)}
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    heading = _tiktok_hook if tiktok else _hook_events
    ev = heading(_clean(hook), min(dur, 3.0) if gameplay else dur, hook_y)
    cap_x = _TIKTOK_TEXT_X if tiktok else W // 2
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        if not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44, min(int(st["size"] * 0.7), int(1500 / max(1, len(line)) * 1.6)))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({cap_x},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cap_width = 740 if tiktok else 820 if gameplay else CAP_MAX_W
        if total > cap_width:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(1 if tiktok else 36, int(size * cap_width / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = cap_x - total / 2
        p = st["pop"]
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:                                                         # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


def _face_crop(face, width, height):
    """Leave a framing margin around the detected box and keep every edge inside the decoded frame."""
    try:
        x, y, w, h = (int(v) for v in face)
    except (TypeError, ValueError, OverflowError):
        return None
    if w <= 8 or h <= 8 or not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
        return None
    padx, pady = max(2, round(w * 0.07)), max(2, round(h * 0.07))
    left, top = max(0, x - padx) // 2 * 2, max(0, y - pady) // 2 * 2
    right, bottom = min(width, x + w + padx + 1) // 2 * 2, min(height, y + h + pady + 1) // 2 * 2
    return left, top, right - left, bottom - top


def gameplay_mask(facecam, width, height):
    face = _face_crop(facecam, width, height)
    return tuple(value / bound for value, bound in zip(face, (width, height, width, height))) if face else None


def _tiktok_filter(region, original_size, facecam):
    x, y, w, h = region
    game_scale = f"scale={w}:{h}:flags=lanczos," if (w, h) != original_size else ""
    game = f"{game_scale}setsar=1,pad={W}:{H}:{x}:{y}:color=black"
    if not facecam:
        return f"[0:v]fps={config.OUT_FPS},{game}[v0];"
    fx, fy, fw, fh = facecam
    face_w, face_h = _TIKTOK_FACE_SIZE
    return (f"[0:v]fps={config.OUT_FPS},split=2[game][face];[game]{game}[b];"
            f"[face]crop={fw}:{fh}:{fx}:{fy},scale={face_w}:{face_h}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2:flags=lanczos,setsar=1[p];"
            f"[b][p]overlay={_TIKTOK_TEXT_X}-w/2:{_TIKTOK_FACE_CENTER}-h/2[v0];")


def _filter(layout, fg_h, facecam=None):
    if layout == "gameplay":
        split = "split=3[bg][game][fc]" if facecam else "split=2[bg][game]"
        layers = (f"[0:v]fps={config.OUT_FPS},{split};"
                  f"[bg]scale=135:240:force_original_aspect_ratio=increase,crop=135:240,boxblur=6:2,"
                  f"scale={W}:{H},eq=brightness=-0.12:saturation=0.75[b];"
                  f"[game]scale={W}:660:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[g];")
        center = 1060 if facecam else 950
        if not facecam:
            return layers + f"[b][g]overlay=(W-w)/2:{center}-h/2,setsar=1[v0];"
        x, y, w, h = facecam
        return (layers + f"[b][g]overlay=(W-w)/2:{center}-h/2[withgame];"
                f"[fc]crop={w}:{h}:{x}:{y},scale=640:384:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[p];"
                "[withgame][p]overlay=(W-w)/2:520-h/2,setsar=1[v0];")
    if layout == "crop":
        return f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0];"
    return (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v0];")


_qsv_ok = None
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int, *, sar=1.0) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    tiktok = clip.get("layout") == FULL_GAMEPLAY
    face = _face_crop(clip.get("facecam"), src_w, src_h) if clip.get("layout") in GAMEPLAY_LAYOUTS else None
    if tiktok:
        layout = FULL_GAMEPLAY
        region = full_gameplay_region(src_w, src_h, sar, facecam=bool(face))
        hook_y, cap_y = (292 if face else 380), _TIKTOK_CAPTION_Y
        filters = _tiktok_filter(region, (src_w, src_h), face)
    else:
        layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"))
        filters = _filter(layout, fg_h, face)
    ass = build_ass(words if clip.get("captions", True) else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y,
                    gameplay=bool(clip.get("gameplay_source") or layout in GAMEPLAY_LAYOUTS), tiktok=tiktok)
    # The clip is made in a short work folder and moved into place afterwards. The caption step of ffmpeg cannot
    # open files whose full path is longer than 260 characters on Windows, which a data folder deep inside other
    # folders easily reaches. Inside the work folder every name is short and relative (a drive colon would also
    # break the filter syntax).
    work = Path(tempfile.mkdtemp(prefix="pk"))
    try:
        with load.heavy_work(writes_video=True):
            files = _render_in(work, src, out_dir, name, start, end, layout, filters, ass)
        if tiktok:
            files["gameplay_region"] = region
            files["gameplay_mask"] = gameplay_mask(clip.get("facecam"), src_w, src_h)
        return files
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, filters, ass: str) -> dict:
    global _qsv_ok
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)
    fc = filters + f"[v0]ass={name}.ass:fontsdir=f[v]"
    threads = load.ffmpeg_threads()
    filters = ["-filter_threads", str(config.FFMPEG_THREADS), "-filter_complex_threads", str(config.FFMPEG_THREADS)] if threads else []
    base = ["ffmpeg", "-y", "-v", "error", *filters, "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", *threads, "-i", str(src),
            "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", *threads, "-pix_fmt"]
    encoders = []
    if not config.LOW_RESOURCE and _qsv_ok is not False:
        encoders.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]))
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"]))
    err = None
    for label, enc in encoders:
        p = subprocess.run(base + enc + [f"{name}.mp4"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=RENDER_SECONDS)
        if p.returncode == 0:
            if label == "qsv":
                _qsv_ok = True
            break
        err = p.stderr[-1200:]
        if label == "qsv":
            _qsv_ok = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    cover_t = min(1.2, (end - start) / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", *threads, "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{name}.{ext}"), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
