"""按分镜表拼 animatic / 粗剪：图片做缓推，视频按入点截取，空素材出占位卡，字卡用 title_card 渲染，
支持黑场渐显/渐隐、闪白、叠化、烧字幕、配乐混音，同时导出 SRT 和带时间码的剪辑表。

用法:
  python animatic.py 分镜表.csv --out 粗剪_v1.mp4 [--music 配乐.wav --music-start 0 --music-gain 0]
                     [--clip-audio] [--burn-subs] [--scope] [--fps 24] [--size 1920x1080] [--only 1-40]

分镜表（UTF-8 CSV，表头用中文；可以用 Excel 另存为"CSV UTF-8"）:
  镜号   必填，任意编号
  时长   秒（0.5）或帧（12f / 12帧）
  素材   图片或视频路径（相对 CSV 所在目录）；"黑场"；留空 = 有字卡就出字卡，没有就出占位卡
  入点   视频素材从第几秒开始取（默认 0）
  转场   切（默认）/ 渐显 / 渐隐 / 闪白 / 叠化，可加帧数并用 + 组合：渐显12+渐隐6、叠化18
  字卡   这一镜要出的文字（\\n 换行，split 预设用 | 分段）；有素材时叠在画面上
  字卡预设  fade / slam / glitch / sweep / split（默认 fade）
  字卡副标  字卡下方小字
  字幕   对白/旁白字幕；相邻镜头字幕相同视为同一句（跨镜头 L-cut）
  画面 景别 运镜  占位卡上显示的说明；图片素材按"运镜"里的 推/拉/左/右/升/降 做缓动
  其余列（段落、声音、备注……）原样保留到剪辑表

输出: 粗剪.mp4、同名 .srt、同名 _剪辑表.csv（每镜起止时间码）
依赖: ffmpeg；pip install numpy opencv-python pillow；同目录的 title_card.py
"""
import argparse, csv, json, os, re, shutil, subprocess, sys, tempfile
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import title_card  # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败:\n" + r.stderr.decode("utf-8", "ignore")[-1500:])


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                         capture_output=True).stdout.strip()
    return bool(out)


def parse_dur(s, fps):
    s = (s or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*(f|帧)", s)
    if m:
        return int(m.group(1)) / fps
    return float(s) if s else 0.0


def parse_trans(s, fps):
    """返回 {'in':(kind,sec), 'out':sec, 'dissolve':sec}"""
    res = {"in": None, "out": 0.0, "dissolve": 0.0}
    for part in re.split(r"[+＋,，\s]+", (s or "").strip()):
        if not part or part in ("切", "硬切", "cut"):
            continue
        m = re.fullmatch(r"(渐显|淡入|渐隐|淡出|渐出|闪白|叠化|fadein|fadeout|flash|dissolve)(\d+)?(f|帧)?", part.lower())
        if not m:
            print(f"  ! 不认识的转场「{part}」，按硬切处理")
            continue
        kind, n = m.group(1), m.group(2)
        sec = int(n) / fps if n else None
        if kind in ("渐显", "淡入", "fadein"):
            res["in"] = ("black", sec or 0.5)
        elif kind in ("闪白", "flash"):
            res["in"] = ("white", sec or 3 / fps)
        elif kind in ("渐隐", "淡出", "渐出", "fadeout"):
            res["out"] = sec or 0.5
        else:
            res["dissolve"] = sec or 0.5
    return res


def font(size, bold=False):
    for p in (["C:/Windows/Fonts/msyhbd.ttc"] if bold else []) + ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                                                                  "/System/Library/Fonts/PingFang.ttc"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def pipe_frames(frames_iter, n, W, H, fps, out):
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
                          "-i", "-", "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                          "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
    for f in frames_iter:
        p.stdin.write(np.ascontiguousarray(f).tobytes())
    p.stdin.close(); p.wait()
    if p.returncode != 0:
        raise RuntimeError("编码失败: " + out)


def image_motion(path, motion, n, W, H, fps, out):
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不了图片: {path}")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    s = max(W / img.shape[1], H / img.shape[0]) * 1.12          # 留 12% 余量给运动
    img = cv2.resize(img, (int(img.shape[1] * s + 1), int(img.shape[0] * s + 1)), interpolation=cv2.INTER_AREA)
    ih, iw = img.shape[:2]
    m = motion or ""
    z0, z1 = (1.0, 1.08) if "推" in m else (1.08, 1.0) if "拉" in m else (1.0, 1.03)
    px = (-1 if "左" in m else 1 if "右" in m else 0)
    py = (-1 if "升" in m or "上" in m else 1 if "降" in m or "下" in m else 0)

    def gen():
        for i in range(n):
            u = i / max(1, n - 1)
            u = u * u * (3 - 2 * u) * 0.6 + u * 0.4                 # 柔和缓动
            z = z0 + (z1 - z0) * u
            vw, vh = W / z, H / z                                   # 视窗大小（缩放后源图像素）
            cx = iw / 2 + px * (iw - vw) / 2 * (u * 2 - 1) * 0.9
            cy = ih / 2 + py * (ih - vh) / 2 * (u * 2 - 1) * 0.9
            M = np.array([[W / vw, 0, -(cx - vw / 2) * W / vw], [0, H / vh, -(cy - vh / 2) * H / vh]], np.float32)
            yield cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    pipe_frames(gen(), n, W, H, fps, out)


def placeholder(row, n, W, H, fps, out, scope=False):
    im = Image.new("RGB", (W, H), (28, 30, 34))
    d = ImageDraw.Draw(im)
    top = int((H - W / 2.39) / 2) if scope else 0          # 宽银幕时内容放进画幅内
    d.rectangle([40, 40 + top, W - 40, H - 40 - top], outline=(80, 84, 92), width=3)
    d.text((80, 70 + top), f"#{row.get('镜号', '')}  {row.get('段落', '')}", fill=(255, 210, 90), font=font(int(H * 0.05), True))
    d.text((80, 70 + top + int(H * 0.08)), f"{row.get('景别', '')}  {row.get('运镜', '')}", fill=(160, 200, 255), font=font(int(H * 0.035)))
    desc = (row.get("画面") or "").strip()
    y, F, line = 70 + top + int(H * 0.16), font(int(H * 0.04)), ""
    for ch in desc:                                                 # 简单按宽度折行
        if F.getlength(line + ch) > W - 200:
            d.text((80, y), line, fill=(230, 230, 230), font=F); y += int(H * 0.055); line = ""
        line += ch
    if line:
        d.text((80, y), line, fill=(230, 230, 230), font=F)
    snd = (row.get("声音") or "").strip()
    if snd:
        d.text((80, H - 40 - top - int(H * 0.08)), "声音：" + snd, fill=(140, 230, 140), font=font(int(H * 0.032)))
    frame = np.asarray(im)
    pipe_frames((frame for _ in range(n)), n, W, H, fps, out)


def subtitle_png(text, W, H, scope, out):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    F = font(int(H * 0.037))
    bottom = H - (H - W / 2.39) / 2 if scope else H
    y = bottom - H * 0.075 - F.size
    for line in reversed(text.split("\\n")):
        w = F.getlength(line)
        d.text(((W - w) / 2, y), line, font=F, fill=(255, 255, 255, 255), stroke_width=max(2, H // 400), stroke_fill=(0, 0, 0, 220))
        y -= F.size * 1.3
    im.save(out)


def finish(src, start, dur, W, H, fps, trans, sub_png, clip_audio, scope, out):
    """统一尺寸/帧率/时长，加渐变、闪白、字幕、宽银幕遮幅，并带一条立体声音轨。"""
    inputs = ["-ss", f"{start:.3f}", "-i", src]
    vf = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}", f"fps={fps}", "setsar=1",
          f"tpad=stop_mode=clone:stop_duration={dur:.3f}", f"trim=duration={dur:.3f}", "setpts=PTS-STARTPTS"]
    if trans["in"]:
        kind, sec = trans["in"]
        vf.append(f"fade=t=in:st=0:d={min(sec, dur):.3f}" + (":color=white" if kind == "white" else ""))
    if trans["out"]:
        sec = min(trans["out"], dur)
        vf.append(f"fade=t=out:st={dur - sec:.3f}:d={sec:.3f}")
    chain = f"[0:v]{','.join(vf)}[v0]"
    idx = 1
    if sub_png:
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", sub_png]
        chain += f";[v0][{idx}:v]overlay=0:0:shortest=1[v1]"; last = "v1"; idx += 1
    else:
        last = "v0"
    if scope:
        bar = int(round((H - W / 2.39) / 2))
        chain += f";[{last}]drawbox=x=0:y=0:w={W}:h={bar}:color=black:t=fill,drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=black:t=fill[vs]"
        last = "vs"
    chain += f";[{last}]format=yuv420p[v]"
    if clip_audio and has_audio(src):
        afx = [f"aresample=48000", "aformat=channel_layouts=stereo", "apad", f"atrim=duration={dur:.3f}", "asetpts=PTS-STARTPTS"]
        if trans["in"]:
            afx.append(f"afade=t=in:st=0:d={min(trans['in'][1], dur):.3f}")
        if trans["out"]:
            afx.append(f"afade=t=out:st={dur - min(trans['out'], dur):.3f}:d={min(trans['out'], dur):.3f}")
        chain += f";[0:a]{','.join(afx)}[a]"
        amap = "[a]"
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = f"{idx}:a"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", chain, "-map", "[v]", "-map", amap,
         "-t", f"{dur:.3f}", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out])


def dissolve(prev, prev_dur, cur, d, fps, out):
    run(["ffmpeg", "-y", "-v", "error", "-i", prev, "-i", cur, "-filter_complex",
         f"[0:v][1:v]xfade=transition=fade:duration={d:.3f}:offset={prev_dur - d:.3f},format=yuv420p[v];"
         f"[0:a][1:a]acrossfade=d={d:.3f}[a]",
         "-map", "[v]", "-map", "[a]", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out])


def tc(sec, fps):
    fr = int(round(sec * fps)); f = int(round(fps))
    return f"{fr // (3600 * f):02d}:{fr // (60 * f) % 60:02d}:{fr // f % 60:02d}:{fr % f:02d}"


def srt_time(sec):
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=24)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--music")
    ap.add_argument("--music-start", type=float, default=0.0, help="从配乐第几秒开始用")
    ap.add_argument("--music-gain", type=float, default=0.0, help="配乐增益 dB")
    ap.add_argument("--clip-audio", action="store_true", help="保留视频素材自带的声音（对白/音效）")
    ap.add_argument("--clip-gain", type=float, default=0.0)
    ap.add_argument("--burn-subs", action="store_true")
    ap.add_argument("--scope", action="store_true", help="加 2.39:1 宽银幕遮幅")
    ap.add_argument("--only", help="只渲染一段镜号范围，如 1-40（按表格行序）")
    ap.add_argument("--keep-temp", action="store_true")
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.lower().split("x"))
    fps = a.fps
    base = os.path.dirname(os.path.abspath(a.csv))
    with open(a.csv, encoding="utf-8-sig", newline="") as fp:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fp)]
    rows = [r for r in rows if r.get("镜号") or r.get("时长")]
    if a.only:
        lo, hi = (int(x) for x in a.only.split("-"))
        rows = rows[lo - 1:hi]
    tmp = tempfile.mkdtemp(prefix="animatic_")
    segs, timeline, t = [], [], 0.0     # segs: [path, dur]
    try:
        for k, r in enumerate(rows):
            dur = parse_dur(r.get("时长"), fps)
            if dur <= 0:
                print(f"  ! 第{k + 1}行 时长为空，跳过"); continue
            n_frames = max(1, int(round(dur * fps)))
            dur = n_frames / fps
            tr = parse_trans(r.get("转场"), fps)
            dis = tr["dissolve"] if segs else 0.0
            ext_dur = dur + dis
            src_field = r.get("素材", "")
            path = os.path.join(base, src_field) if src_field and src_field not in ("黑场", "black") else ""
            ext = os.path.splitext(path)[1].lower()
            start = 0.0
            card = r.get("字卡", "")
            raw = os.path.join(tmp, f"raw_{k:04d}.mp4")
            label = f"#{r.get('镜号', k + 1)} {dur:.2f}s"
            if src_field in ("黑场", "black"):
                run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={fps}:d={ext_dur:.3f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", raw])
                src = raw; label += " 黑场"
            elif path and not os.path.exists(path):
                print(f"  ! 找不到素材 {src_field}，改出占位卡")
                placeholder(r, int(round(ext_dur * fps)), W, H, fps, raw, a.scope); src = raw; label += " 占位(缺素材)"
            elif path and ext in IMAGE_EXT:
                if card:
                    title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None, bg=path)
                else:
                    image_motion(path, r.get("运镜", ""), int(round(ext_dur * fps)), W, H, fps, raw)
                src = raw; label += " 图"
            elif path and ext in VIDEO_EXT:
                start = max(0.0, float(r.get("入点") or 0) - dis)
                if card:
                    cut = os.path.join(tmp, f"cut_{k:04d}.mp4")
                    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-t", f"{ext_dur:.3f}",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-an", cut])
                    title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None, bg=cut)
                    src, start = raw, 0.0
                else:
                    src = path
                label += " 视频"
            elif path:
                raise SystemExit(f"不支持的素材类型: {src_field}")
            elif card:
                title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None)
                src = raw; label += " 字卡"
            else:
                placeholder(r, int(round(ext_dur * fps)), W, H, fps, raw, a.scope); src = raw; label += " 占位"
            sub_png = None
            if a.burn_subs and r.get("字幕"):
                sub_png = os.path.join(tmp, f"sub_{k:04d}.png")
                subtitle_png(r["字幕"], W, H, a.scope, sub_png)
            seg = os.path.join(tmp, f"seg_{k:04d}.mp4")
            finish(src, start, ext_dur, W, H, fps, tr, sub_png, a.clip_audio and src == path, a.scope, seg)
            if dis > 0:
                merged = os.path.join(tmp, f"dis_{k:04d}.mp4")
                dissolve(segs[-1][0], segs[-1][1], seg, dis, fps, merged)
                segs[-1] = [merged, segs[-1][1] + dur]
                label += f" 叠化{dis:.2f}s"
            else:
                segs.append([seg, dur])
            timeline.append({**r, "_start": t, "_end": t + dur})
            print(f"  {label:<28} {tc(t, fps)} → {tc(t + dur, fps)}")
            t += dur
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as fp:
            for s, _ in segs:
                fp.write(f"file '{s.replace(os.sep, '/')}'\n")
        joined = os.path.join(tmp, "joined.mp4")
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
        out = a.out
        if a.music:
            run(["ffmpeg", "-y", "-v", "error", "-i", joined, "-ss", f"{a.music_start:.3f}", "-i", a.music, "-filter_complex",
                 f"[0:a]volume={a.clip_gain}dB[c];[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={a.music_gain}dB,"
                 f"atrim=duration={t:.3f}[m];[c][m]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out])
        else:
            shutil.copyfile(joined, out)
    finally:
        if not a.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
    stem = os.path.splitext(a.out)[0]
    # 字幕：相邻相同合并
    subs = []
    for x in timeline:
        s = x.get("字幕", "")
        if not s:
            continue
        if subs and subs[-1][2] == s and abs(subs[-1][1] - x["_start"]) < 1e-6:
            subs[-1][1] = x["_end"]
        else:
            subs.append([x["_start"], x["_end"], s])
    with open(stem + ".srt", "w", encoding="utf-8") as fp:
        for i, (s, e, txt) in enumerate(subs, 1):
            fp.write(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{txt.replace(chr(92) + 'n', chr(10))}\n\n")
    keys = []
    for x in timeline:
        keys += [k for k in x if not k.startswith("_") and k not in keys]
    with open(stem + "_剪辑表.csv", "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["入点时间码", "出点时间码", "起(秒)", "止(秒)"] + keys)
        for x in timeline:
            w.writerow([tc(x["_start"], fps), tc(x["_end"], fps), f"{x['_start']:.3f}", f"{x['_end']:.3f}"] + [x.get(k, "") for k in keys])
    print(f"完成：{len(timeline)} 镜，总长 {t:.2f}s → {a.out}")
    print(f"字幕 {len(subs)} 条 → {stem}.srt；剪辑表 → {stem}_剪辑表.csv")


if __name__ == "__main__":
    main()
