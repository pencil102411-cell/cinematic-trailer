"""按分镜表拼 animatic / 粗剪：图片做缓推，视频按入点截取，空素材出占位卡，字卡用 title_card 渲染，
支持黑场渐显/渐隐、闪白、黑闪、故障、冲推、叠化、烧字幕、配乐混音，同时导出 SRT 和带时间码的剪辑表。

用法:
  python animatic.py 分镜表.csv --out 粗剪_v1.mp4 [--music 配乐.wav --music-start 0 --music-gain 0]
                     [--clip-audio] [--burn-subs] [--scope] [--label] [--bpm 128]
                     [--fps 24] [--size 1920x1080] [--only 1-40] [--check]

分镜表（UTF-8 CSV，表头用中文；可以用 Excel 另存为"CSV UTF-8"）:
  镜号   必填，任意编号
  时长   秒（0.5 / 0.5秒）、帧（12f / 12帧）或拍（2拍 / 2b / 半拍，要给 --bpm）；"4拍 / 1.88秒"只取斜杠前面
  素材   图片或视频路径（相对 CSV 所在目录）；"黑场"；留空 = 有字卡就出字卡，没有就出占位卡
  入点   视频素材从第几秒开始取（默认 0。生成视频第 0→1 帧常跳一下，写 0 会提醒，建议从 0.05 起）
  转场   切（默认）/ 渐显 / 渐隐 / 闪白 / 叠化，可加帧数并用 + 组合：渐显12+渐隐6、叠化18
         开头效果：黑闪N（开头 N 帧黑）/ 故障N（开头 N 帧故障，不写数字是整镜）/ 冲推（开头 3 帧猛推近，冲推4 = 4 帧）
  字卡   这一镜要出的文字（\\n 换行，split 预设用 | 分段）；有素材时叠在画面上
  字卡预设  fade / slam / glitch / sweep / split（默认 fade）
  字卡副标  字卡下方小字
  字幕   对白/旁白字幕；相邻镜头字幕相同视为同一句（跨镜头 L-cut）
  画面 景别 运镜  占位卡上显示的说明；图片素材按"运镜"里的 推/拉/左/右/升/降 做缓动
  其余列（段落、制作、声音、生成条目、备注……）原样保留到剪辑表

  --label  镜头号版：每镜左下角压"#镜号 + 这一镜在片里第几秒"（加 --scope 时放进下方黑边），给逐刀返修用，成片别加
  --check  只做开剪前检查（时长、素材在不在、入点、素材够不够长），不出片
  每镜的帧数按累计时间取整，剪多少镜都不会慢慢偏离节拍。时长写拍时，把 --music-start 设成配乐里
  第一镜要对齐的那一拍的秒数，切点就都落在拍上。--only 只出一段时，镜头号版的秒数仍按整片算。

输出: 粗剪.mp4、同名 .srt、同名 _剪辑表.csv（每镜起止时间码）
依赖: ffmpeg；pip install numpy opencv-python pillow；同目录的 title_card.py、fx.py
"""
import argparse, csv, json, os, re, shutil, subprocess, sys, tempfile
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import title_card  # noqa: E402
import fx  # noqa: E402

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


def media_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def parse_dur(s, fps, bpm=None):
    """时长 → 秒。认 0.5 / 0.5秒 / 0.5s、12f / 12帧、2拍 / 2b / 半拍；"4拍 / 1.88秒"只取斜杠前面。写错时抛 ValueError。"""
    s = re.split(r"[/／]", (s or "").strip())[0].strip().lower().replace(" ", "")
    if not s:
        return 0.0
    if s in ("半拍", "半b"):
        s = "0.5拍"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(f|帧|拍|b|s|秒)?", s)
    if not m:
        raise ValueError(f"时长「{s}」看不懂，写成 0.5（秒）、12f（帧）或 2拍")
    v, u = float(m.group(1)), m.group(2) or "s"
    if u in ("f", "帧"):
        return v / fps
    if u in ("拍", "b"):
        if not bpm:
            raise ValueError(f"时长「{s}」是按拍写的，要给 --bpm")
        return v * 60.0 / bpm
    return v


def parse_trans(s, fps):
    """返回 {'in':(kind,sec), 'out':sec, 'dissolve':sec, 'fx':[(kind,帧数)], 'bad':[不认识的写法]}"""
    res = {"in": None, "out": 0.0, "dissolve": 0.0, "fx": [], "bad": []}
    for part in re.split(r"[+＋,，\s]+", (s or "").strip()):
        if not part or part in ("切", "硬切", "cut"):
            continue
        m = re.fullmatch(r"(渐显|淡入|渐隐|淡出|渐出|闪白|叠化|黑闪|故障|冲推|fadein|fadeout|flash|dissolve|black|glitch|punch)(\d+)?(f|帧)?",
                         part.lower())
        if not m:
            res["bad"].append(part)
            continue
        kind, n = m.group(1), m.group(2)
        sec = int(n) / fps if n else None
        if kind in ("渐显", "淡入", "fadein"):
            res["in"] = ("black", sec or 0.5)
        elif kind in ("闪白", "flash"):
            res["in"] = ("white", sec or 3 / fps)
        elif kind in ("渐隐", "淡出", "渐出", "fadeout"):
            res["out"] = sec or 0.5
        elif kind in ("黑闪", "black"):
            res["fx"].append(("black", int(n) if n else 2))
        elif kind in ("故障", "glitch"):
            res["fx"].append(("glitch", int(n) if n else 0))
        elif kind in ("冲推", "punch"):
            res["fx"].append(("punch", int(n) if n else 3))
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


def placeholder(row, n, W, H, fps, out, scope=False, missing=None):
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
    if missing:
        d.text((80, H - 40 - top - int(H * 0.14)), "缺素材：" + missing, fill=(255, 120, 110), font=font(int(H * 0.032)))
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


def label_png(text, W, H, scope, out):
    """镜头号版的标签：左下角"#镜号  第几秒"。有 2.39 遮幅时放进下方黑边，不挡画面。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    F = font(max(14, int(H * 0.032)), True)
    bar = int(round((H - W / 2.39) / 2)) if scope else 0
    x, th, tw = int(W * 0.02), F.size, F.getlength(text)
    if bar > th * 1.4:
        y = H - bar + (bar - th) // 2
    else:
        y = H - bar - int(H * 0.03) - th
        d.rectangle([x - 8, y - 6, x + tw + 8, y + th + 8], fill=(0, 0, 0, 150))
    d.text((x, y), text, font=F, fill=(255, 210, 90, 255))
    im.save(out)


def dress_chain(last, idx, inputs, dur, W, H, fps, sub_png, lab_png, scope):
    """在画面链后面接：字幕 → 宽银幕遮幅 → 镜头号。返回 (chain, last, idx)。
    图层按片子的帧率读、比画面多给 2 帧：按默认 25 帧读再用 shortest，每镜会被吃掉最后一帧。"""
    chain = ""
    layer = lambda png: ["-loop", "1", "-framerate", f"{fps}", "-t", f"{dur + 2 / fps:.3f}", "-i", png]
    if sub_png:
        inputs += layer(sub_png)
        chain += f";[{last}][{idx}:v]overlay=0:0:eof_action=repeat[vsub]"; last = "vsub"; idx += 1
    if scope:
        bar = int(round((H - W / 2.39) / 2))
        chain += f";[{last}]drawbox=x=0:y=0:w={W}:h={bar}:color=black:t=fill,drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=black:t=fill[vs]"
        last = "vs"
    if lab_png:
        inputs += layer(lab_png)
        chain += f";[{last}][{idx}:v]overlay=0:0:eof_action=repeat[vlab]"; last = "vlab"; idx += 1
    return chain, last, idx


def finish(src, start, dur, W, H, fps, trans, sub_png, lab_png, clip_audio, scope, out):
    """统一尺寸/帧率/时长，加渐变、闪白、字幕、宽银幕遮幅、镜头号，并带一条立体声音轨。"""
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
    more, last, idx = dress_chain("v0", 1, inputs, dur, W, H, fps, sub_png, lab_png, scope)
    chain += more + f";[{last}]format=yuv420p[v]"
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


def dress(src, dur, W, H, fps, sub_png, lab_png, scope, out):
    """给已经做好效果的一镜再叠字幕、遮幅、镜头号（声音原样带过去）。"""
    inputs = ["-i", src]
    more, last, _ = dress_chain("v0", 1, inputs, dur, W, H, fps, sub_png, lab_png, scope)
    chain = (f"[0:v]fps={fps},tpad=stop_mode=clone:stop_duration={dur:.3f},trim=duration={dur:.3f},setpts=PTS-STARTPTS[v0]"
             + more + f";[{last}]format=yuv420p[v];[0:a]apad,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[a]")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", chain, "-map", "[v]", "-map", "[a]",
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


def preflight(rows, base, fps, bpm):
    """开剪前把每一镜过一遍：算帧数（按累计时间取整）、认素材、查入点和长度。返回 (plan, problems)。
    problems 里每条是 (行序号, 严重程度 'err'/'warn', 说明)。"""
    plan, problems, t_exact, prev_n = [], [], 0.0, 0
    for k, r in enumerate(rows):
        tag = f"第{k + 1}行 #{r.get('镜号') or '?'}"
        try:
            dur = parse_dur(r.get("时长"), fps, bpm)
        except ValueError as e:
            problems.append((k, "err", f"{tag}：{e}")); continue
        if dur <= 0:
            problems.append((k, "warn", f"{tag}：时长为空，跳过")); continue
        start_f = int(round(t_exact * fps))
        t_exact += dur
        n = int(round(t_exact * fps)) - start_f
        if n < 1:
            problems.append((k, "warn", f"{tag}：时长 {dur:.3f}s 不足 1 帧，跳过")); continue
        tr = parse_trans(r.get("转场"), fps)
        for b in tr["bad"]:
            problems.append((k, "warn", f"{tag}：不认识的转场「{b}」，按硬切处理"))
        dis_n = int(round(tr["dissolve"] * fps))
        if dis_n and prev_n and dis_n >= prev_n:
            problems.append((k, "warn", f"{tag}：叠化 {dis_n} 帧比上一镜还长，缩到 {prev_n - 1} 帧"))
            dis_n = prev_n - 1
        src = r.get("素材", "")
        path = os.path.join(base, src) if src and src not in ("黑场", "black") else ""
        ext = os.path.splitext(path)[1].lower()
        item = {"k": k, "row": r, "start_f": start_f, "n": n, "tr": tr, "dis_n": dis_n, "src": src, "path": path, "in": 0.0}
        if src in ("黑场", "black"):
            item["kind"] = "black"
        elif path and not os.path.exists(path):
            item["kind"] = "missing"
            problems.append((k, "warn", f"{tag}：找不到素材 {src}，改出占位卡"))
        elif path and ext in IMAGE_EXT:
            item["kind"] = "image"
        elif path and ext in VIDEO_EXT:
            item["kind"] = "video"
            try:
                item["in"] = float(r.get("入点") or 0)
            except ValueError:
                problems.append((k, "err", f"{tag}：入点「{r.get('入点')}」不是秒数")); continue
            if item["in"] == 0 and not dis_n and not os.path.basename(path).upper().startswith("L1_"):   # L1_ 是 fx.py 做的片段，从头用没问题
                problems.append((k, "warn", f"{tag}：入点是 0。生成视频第 0→1 帧常跳一下，建议从 0.05 起"))
            total = media_dur(path)
            need = (n + dis_n) / fps
            avail = (total or 0) - max(0.0, item["in"] - dis_n / fps)
            if total is not None and avail < need - 0.5 / fps:
                frozen = int(round((need - max(avail, 0)) * fps))
                problems.append((k, "warn", f"{tag}：{src} 只有 {total:.2f}s，从 {item['in']:.2f}s 起不够 {need:.2f}s，"
                                            f"最后约 {frozen} 帧是定格"))
        elif path:
            problems.append((k, "err", f"{tag}：不支持的素材类型 {src}")); continue
        elif r.get("字卡"):
            item["kind"] = "card"
        else:
            item["kind"] = "placeholder"
        plan.append(item)
        prev_n = n
    return plan, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--out", help="输出文件（--check 时可以不给）")
    ap.add_argument("--fps", type=float, default=24)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--music")
    ap.add_argument("--music-start", type=float, default=0.0, help="从配乐第几秒开始用")
    ap.add_argument("--music-gain", type=float, default=0.0, help="配乐增益 dB")
    ap.add_argument("--clip-audio", action="store_true", help="保留视频素材自带的声音（对白/音效）")
    ap.add_argument("--clip-gain", type=float, default=0.0)
    ap.add_argument("--burn-subs", action="store_true")
    ap.add_argument("--scope", action="store_true", help="加 2.39:1 宽银幕遮幅")
    ap.add_argument("--label", action="store_true", help="镜头号版：每镜左下角压镜号和片里秒数（返修用，成片别加）")
    ap.add_argument("--bpm", type=float, help="每分钟多少拍（时长写成 2拍 时需要）")
    ap.add_argument("--only", help="只渲染一段镜号范围，如 1-40（按表格行序）")
    ap.add_argument("--check", action="store_true", help="只做开剪前检查，不出片")
    ap.add_argument("--keep-temp", action="store_true")
    a = ap.parse_args()
    if not a.out and not a.check:
        ap.error("要给 --out（或者用 --check 只做检查）")
    W, H = (int(x) for x in a.size.lower().split("x"))
    fps = a.fps
    base = os.path.dirname(os.path.abspath(a.csv))
    with open(a.csv, encoding="utf-8-sig", newline="") as fp:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fp)]
    rows = [r for r in rows if r.get("镜号") or r.get("时长")]
    lo, hi = 1, len(rows)
    if a.only:
        lo, hi = (int(x) for x in a.only.split("-"))
    plan, problems = preflight(rows, base, fps, a.bpm)
    sel = [p for p in plan if lo - 1 <= p["k"] <= hi - 1]
    total_f = sum(p["n"] for p in plan)
    print(f"开剪前检查：{len(plan)} 镜，总长 {total_f / fps:.2f}s（{total_f} 帧 @{fps:g}fps）"
          + (f"；这次只出第 {lo}–{hi} 行，{len(sel)} 镜" if a.only else ""))
    errs = [m for k, lvl, m in problems if lvl == "err" and lo - 1 <= k <= hi - 1]
    for k, lvl, m in problems:
        print(("  ✗ " if lvl == "err" else "  ! ") + m + ("" if lo - 1 <= k <= hi - 1 else "（不在这次范围内）"))
    if errs:
        raise SystemExit(f"有 {len(errs)} 处要先改（上面打 ✗ 的），没有出片")
    if not problems:
        print("  没发现问题")
    if a.check:
        return
    if not sel:
        raise SystemExit("这个范围里没有要出的镜头")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="animatic_")
    segs, timeline = [], []     # segs: [path, dur]
    t0 = sel[0]["start_f"] / fps
    try:
        for it in sel:
            r, k, n = it["row"], it["k"], it["n"]
            dur = n / fps
            tr = it["tr"]
            dis_n = it["dis_n"] if segs else 0
            dis = dis_n / fps
            ext_n = n + dis_n
            ext_dur = ext_n / fps
            start = 0.0
            card = r.get("字卡", "")
            raw = os.path.join(tmp, f"raw_{k:04d}.mp4")
            abs_t = it["start_f"] / fps
            label = f"#{r.get('镜号', k + 1)} {dur:.2f}s"
            kind = it["kind"]
            if kind == "black":
                run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={fps}:d={ext_dur:.3f}",
                     "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", raw])
                src = raw; label += " 黑场"
            elif kind == "missing":
                placeholder(r, ext_n, W, H, fps, raw, a.scope, missing=it["src"]); src = raw; label += " 占位(缺素材)"
            elif kind == "image":
                if card:
                    title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None, bg=it["path"])
                else:
                    image_motion(it["path"], r.get("运镜", ""), ext_n, W, H, fps, raw)
                src = raw; label += " 图"
            elif kind == "video":
                start = max(0.0, it["in"] - dis)
                if card:
                    cut = os.path.join(tmp, f"cut_{k:04d}.mp4")
                    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", it["path"], "-t", f"{ext_dur:.3f}",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-an", cut])
                    title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None, bg=cut)
                    src, start = raw, 0.0
                else:
                    src = it["path"]
                label += " 视频"
            elif kind == "card":
                title_card.render(card, raw, r.get("字卡预设") or "fade", ext_dur, fps, (W, H), r.get("字卡副标") or None)
                src = raw; label += " 字卡"
            else:
                placeholder(r, ext_n, W, H, fps, raw, a.scope); src = raw; label += " 占位"
            sub_png = lab_png = None
            if a.burn_subs and r.get("字幕"):
                sub_png = os.path.join(tmp, f"sub_{k:04d}.png")
                subtitle_png(r["字幕"], W, H, a.scope, sub_png)
            if a.label:
                lab_png = os.path.join(tmp, f"lab_{k:04d}.png")
                label_png(f"#{r.get('镜号', k + 1)}  {abs_t:.2f}s", W, H, a.scope, lab_png)
            seg = os.path.join(tmp, f"seg_{k:04d}.mp4")
            clip_audio = a.clip_audio and src == it["path"]
            if tr["fx"]:                       # 开头效果要在字幕、遮幅、镜头号之前做，不然会把它们一起推近、撕裂
                plain, fxd = os.path.join(tmp, f"plain_{k:04d}.mp4"), os.path.join(tmp, f"fx_{k:04d}.mp4")
                finish(src, start, ext_dur, W, H, fps, tr, None, None, clip_audio, False, plain)
                fx.head_effects(plain, fxd, tr["fx"], ext_n, W, H, fps)
                dress(fxd, ext_dur, W, H, fps, sub_png, lab_png, a.scope, seg)
                label += " +" + "+".join(f"{kd}{N or ''}" for kd, N in tr["fx"])
            else:
                finish(src, start, ext_dur, W, H, fps, tr, sub_png, lab_png, clip_audio, a.scope, seg)
            if dis_n > 0:
                merged = os.path.join(tmp, f"dis_{k:04d}.mp4")
                dissolve(segs[-1][0], segs[-1][1], seg, dis, fps, merged)
                segs[-1] = [merged, segs[-1][1] + dur]
                label += f" 叠化{dis:.2f}s"
            else:
                segs.append([seg, dur])
            rel = abs_t - t0
            timeline.append({**r, "_start": rel, "_end": rel + dur})
            print(f"  {label:<28} {tc(rel, fps)} → {tc(rel + dur, fps)}")
        t = sum(d for _, d in segs)
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
    print(f"完成：{len(timeline)} 镜，总长 {t:.2f}s → {a.out}" + ("（镜头号版，成片别用）" if a.label else ""))
    print(f"字幕 {len(subs)} 条 → {stem}.srt；剪辑表 → {stem}_剪辑表.csv")


if __name__ == "__main__":
    main()
