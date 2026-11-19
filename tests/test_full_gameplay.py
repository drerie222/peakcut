"""Render synthetic gameplay through the legacy and Full Gameplay / TikTok paths."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

work = tempfile.TemporaryDirectory(prefix="pc-full-gameplay-")
os.environ.update(PEAKCUT_DATA=work.name, DATABASE_URL="", PEAKCUT_LOW_RESOURCE="1",
                  PEAKCUT_OUT_FPS="12", PEAKCUT_AI_KEY="", PEAKCUT_AI_MODELS="", BUFFER_API_KEY="")
sys.path.insert(0, ".")
from studio import auth, buffer, creators, ctx, db, jobs, live, media, quality, render, speaker

failures = []
CAM = [24, 24, 356, 238]
END = 6.5
TITLE = "THE LAST CORNER"
WORDS = [{"w": "What", "s": 2, "e": 2.29}, {"w": "a", "s": 2.30, "e": 2.50},
         {"w": "finish!", "s": 2.51, "e": 3.30}]


def check(name, value):
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        failures.append(name)


def fixture(root):
    game = Image.new("RGB", (1280, 720), "#233050")
    draw = ImageDraw.Draw(game)
    for y in range(392):
        draw.line((0, y, 1280, y), fill=(35 + y // 6, 48 + y // 9, 80 + y // 10))
    draw.ellipse((702, 55, 805, 158), fill="#e9af8a")
    for x in range(0, 1280, 72):
        top = 250 + (x * 17 % 93)
        draw.rectangle((x, top, x + 59, 401), fill="#313850")
        for row in range(top + 13, 388, 17):
            for col in range(x + 9, x + 55, 12):
                if (col * row) % 5:
                    draw.rectangle((col, row, col + 4, row + 5), fill="#6f7086")
    draw.rectangle((0, 400, 1280, 720), fill="#283844")
    draw.polygon(((557, 389), (716, 389), (1223, 720), (64, 720)), fill="#3c454c")
    for j in range(9):
        y = 416 + j * (j + 2) * 3
        draw.polygon(((639, y), (645 + j, y), (654 + j, y + 12), (636 - j, y + 12)), fill="#f4d2a5")
    draw.line(((557, 391), (65, 719)), width=7, fill="#cbb39e")
    draw.line(((716, 391), (1223, 719)), width=7, fill="#cbb39e")
    draw.rectangle((0, 500, 51, 569), fill="#df545f")
    draw.rounded_rectangle((1083, 526, 1278, 668), radius=12, fill="#235649", outline="#52e5a0", width=4)
    draw.line(((1097, 637), (1150, 596), (1171, 614), (1258, 549)), fill="#c1f4e2", width=6)
    draw.rectangle((1208, 638, 1279, 664), fill="#52e5a0")
    game.save(root / "game.png")

    cam = Image.new("RGB", (CAM[2], CAM[3]), "#5e626a")
    face = ImageDraw.Draw(cam)
    face.rectangle((7, 7, 348, 230), fill="#383143")
    face.ellipse((86, 158, 269, 287), fill="#447d88")
    face.ellipse((126, 32, 226, 165), fill="#e6a78f")
    face.pieslice((122, 25, 230, 109), 178, 356, fill="#4b3338")
    face.ellipse((148, 89, 157, 98), fill="#392630")
    face.ellipse((194, 89, 203, 98), fill="#392630")
    face.arc((161, 107, 192, 135), start=4, end=167, fill="#683d45", width=3)
    cam.save(root / "camera.png")

    car = Image.new("RGBA", (218, 112))
    draw = ImageDraw.Draw(car)
    draw.rounded_rectangle((4, 30, 211, 105), radius=21, fill="#294a85")
    draw.polygon(((35, 45), (68, 9), (151, 9), (188, 45)), fill="#7b9cbb")
    draw.rounded_rectangle((7, 43, 208, 94), radius=12, fill="#3982b9")
    draw.rounded_rectangle((16, 58, 200, 68), radius=5, fill="#acdaee")
    draw.rectangle((29, 83, 61, 92), fill="#ff8380")
    draw.rectangle((158, 83, 188, 92), fill="#ff8380")
    car.save(root / "car.png")

    path = root / "synthetic.mp4"
    graph = ("[0:v][1:v]overlay=x='260+mod(t*650,550)':y=495[g];"
             "[g][1:v]overlay=x='850-mod(t*620,620)':y=405,"
             "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,0.75)'[intro];"
             f"[intro][2:v]overlay={CAM[0]}:{CAM[1]}[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-filter_complex_threads", "1", "-filter_threads", "1",
                    "-loop", "1", "-framerate", "12", "-i", str(root / "game.png"), "-i", str(root / "car.png"),
                    "-i", str(root / "camera.png"), "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000",
                    "-filter_complex", graph, "-map", "[v]", "-map", "3:a:0", "-t", "7",
                    "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
                   check=True, timeout=30)
    return path


def frame(path, at, output):
    media.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-threads", "1", "-i", str(path),
               "-frames:v", "1", "-threads", "1", str(output)], timeout=16)
    with Image.open(output) as picture:
        return np.array(picture.convert("RGB"))


def clip_for(layout):
    return dict(id="moment", status="queued", start=0, end=END, title=TITLE, hook=TITLE,
                score=93, peak_sec=3.0, evidence="visual", description="", hashtags=[], layout=layout,
                facecam=CAM, facecam_checked=True, captions=True, style="clean")


def test_defaults():
    check("automatic uses Full Gameplay for games and the familiar default for other videos",
          render.choose_layout("auto", True) == render.FULL_GAMEPLAY and render.choose_layout("auto", False) == "blur")
    check("the exact editor option and all legacy options are available",
          render.LAYOUTS[render.FULL_GAMEPLAY] == "Full Gameplay / TikTok"
          and {"gameplay", "blur", "zoom", "crop"} <= render.LAYOUTS.keys())
    check("the upload and remake configuration accepts the real new layout identifier",
          jobs.clean_options({"layout": render.FULL_GAMEPLAY})["layout"] == render.FULL_GAMEPLAY)
    creator = creators.create({"name": "Legacy preset"}, watchlist={"key": "test", "order": 0})
    creators._write({**creator, "layout": "gameplay"})
    job = jobs.create({"type": "file", "name": "Highlight"}, {}, enqueue=False, creator=creator["id"])
    check("legacy automatic watchlist settings get the new default without changing the saved layout",
          job["options"]["layout"] == render.FULL_GAMEPLAY and creators.get(creator["id"])["layout"] == "gameplay")
    selected = creators.update(creator["id"], {"layout": "gameplay"})
    check("an explicitly selected old layout remains in effect",
          render.choose_layout(creators.layout_preference(selected), True) == "gameplay")
    rec = jobs.create({"type": "live", "name": "GTA V"}, {}, enqueue=False, kind="live")
    session = live.Session(rec, creators.DEFAULTS)
    check("new live gaming clips resolve their default when the game becomes known",
          session._clip_layout() == render.FULL_GAMEPLAY)
    with patch.object(jobs, "submit_live"):
        session._add_clip({"start": 3, "end": 5})
    check("live queue entries record the concrete layout", rec["clips"][0]["layout"] == render.FULL_GAMEPLAY)
    old = {**clip_for("gameplay"), "status": "done", "version": 1}
    job["clips"].append(old)
    jobs.request_render(job["id"], old["id"], {"layout": render.FULL_GAMEPLAY})
    check("an existing clip can switch to the new layout through the remake queue",
          old["layout"] == render.FULL_GAMEPLAY and old["status"] == "queued")


def test_dimensions():
    check("1080p gameplay uses the canvas width at 16:9",
          render.full_gameplay_region(1920, 1080)[2:] == (1080, 608))
    check("low resolution gameplay avoids upscaling",
          render.full_gameplay_region(640, 360)[2:] == (640, 360))
    check("other source shapes stay within the canvas without cropping",
          render.full_gameplay_region(1080, 1920, facecam=True)[2:] == (360, 640))
    _, _, w, h = render.full_gameplay_region(720, 576, 64 / 45)
    check("anamorphic video is converted to square pixels at its display ratio", w == 720 and abs(h - 405) <= 1)


def test_first_frame(root):
    path = root / "single-black-frame.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24",
                    "-vf", "drawbox=color=black:t=fill:enable='eq(n,0)'", "-t", "2", "-threads", "1", str(path)],
                   check=True, timeout=12)
    cut = quality.opening(path, 0, 2, strict=True)
    check("opening validation also detects a single native black first frame", cut is not None and 0 < cut < .5)


def test_render(root, source):
    job = jobs.create({"type": "file", "name": "Synthetic race", "path": str(source)},
                      {"layout": render.FULL_GAMEPLAY}, enqueue=False)
    job.update(source_path=str(source), duration=7)
    clip = clip_for(render.FULL_GAMEPLAY)
    job["clips"].append(clip)
    with patch.object(speaker, "captions", return_value=(WORDS, "isolated_mic")):
        jobs.render_one(job, clip, [])
    after = Path(job["dir"]) / "clips" / "moment.mp4"
    check("black gameplay is trimmed even when its embedded facecam is already visible",
          clip["status"] == "done" and .65 < clip.get("trimmed_opening", 0) < 1.25)
    check("the completed new layout passes the normal Buffer quality gate", not buffer.review_issues(job, clip))
    check("actual render stays at 1080 × 1920", (media.probe(after)["width"], media.probe(after)["height"]) == (1080, 1920))

    after_png = root / "after.png"
    picture = frame(after, 1.9, after_png)
    x, y, w, h = clip["gameplay_region"]
    check("the encoded layout has deliberately black upper and lower bands",
          np.max(picture[108:168]) < 8 and np.max(picture[1640:1760]) < 8)
    check("pixels from both outside gameplay edges survive the vertical export",
          picture[y + int(h * .74), x + 12, 0] > 140 and picture[y + int(h * .905), x + w - 14, 1] > 142)
    check("the optional facecam is in a separate area above the gameplay",
          np.mean(picture[430:605, 365:610]) > 50 and np.max(picture[632:667]) < 10)
    ass = after.with_suffix(".ass").read_text(encoding="utf-8")
    anchors = [(int(x), int(y)) for x, y in re.findall(r"\\pos\((\d+),(\d+)\)", ass)]
    title_anchor = next((p for p in anchors if p[1] < y), (0, 0))
    check("the short title is above gameplay and left of the right-side controls",
          title_anchor[0] == 490 and 200 <= title_anchor[1] <= 420 and TITLE in ass and "0:00:03.00,Hook" in ass)
    check("caption anchors are below the game and above the bottom overlay",
          any(y + h < py <= 1450 and 85 <= px <= 880 for px, py in anchors))
    crowded = render.build_ass([], 0, 5, "W" * 60, "hype", [], 292, 1420, gameplay=True, tiktok=True)
    check("long titles stay legible and the title and captions share one typeface",
          "…" in crowded and "\\fs68" in crowded and "Style: Cap,Poppins ExtraBold," in crowded)

    legacy = {**clip_for("gameplay"), "start": clip["start"]}
    previous = render.render_clip(source, root, "legacy", legacy, WORDS, 1280, 720)
    frame(root / previous["file"], 1.9, root / "before.png")
    check("the previous blurred gameplay renderer remains independently selectable", previous["layout"] == "gameplay")

    blocked = render.render_clip(source, root, "black", clip_for(render.FULL_GAMEPLAY), WORDS, 1280, 720)
    result = quality.review(root / blocked["file"], {**clip_for(render.FULL_GAMEPLAY), **blocked}, 1, gameplay=True)
    check("bright hook and facecam cannot hide a black opening from the rendered-file check",
          any("opening frames are black" in issue for issue in result["issues"]))

    centered = render.render_clip(source, root, "no-face",
                                  {**clip_for(render.FULL_GAMEPLAY), "facecam": None, "start": 1.0}, [], 1280, 720)
    check("the same layout centers gameplay when no separate facecam is rendered",
          centered["gameplay_region"] == render.full_gameplay_region(1280, 720))


def main():
    db.init()
    render.install_fonts()
    root = Path(os.environ.get("PEAKCUT_LAYOUT_EXAMPLES") or work.name)
    root.mkdir(parents=True, exist_ok=True)
    with ctx.as_user(auth.local_user().id):
        test_defaults()
        test_dimensions()
        test_first_frame(root)
        test_render(root, fixture(root))
    return bool(failures)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        db.engine.dispose()
        work.cleanup()
