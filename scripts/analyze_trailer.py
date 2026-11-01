"""拆解一支参考预告片：逐帧检测切点/黑场/闪白/叠化/运镜，音频检测响度/冲击/静音/节拍，
输出镜头表、统计摘要、接触表和时间线图。

用法:
  python analyze_trailer.py 预告片.mp4 --out 输出目录 [--transcribe] [--every 1.5]

  --transcribe   用 faster-whisper 转写台词（需另装：pip install faster-whisper）
  --every SEC    片子几乎没有切点（纯字卡模板）时，接触表改为每隔 SEC 秒抽一帧

依赖: ffmpeg/ffprobe 在 PATH；pip install numpy opencv-python scipy pillow
输出: frames.json audio.json shots.json summary.txt timeline.png sheets/sheet_XX.jpg [speech.json]
"""
import argparse, json, os, subprocess, sys
import numpy as np
import cv2
from scipy.signal import stft, find_peaks
from scipy.ndimage import uniform_filter1d, maximum_filter1d
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")
AW, AH = 192, 108          # 逐帧分析用的缩略尺寸
SR, NFFT, HOP = 22050, 2048, 512


def font(size):
    for p in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def probe(path):
    j = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                            "-show_entries", "stream=r_frame_rate,width,height:format=duration",
                                            "-of", "json", path]))
    num, den = j["streams"][0]["r_frame_rate"].split("/")
    return float(num) / float(den), float(j["format"]["duration"]), j["streams"][0]["width"], j["streams"][0]["height"]


def frame_stream(path, w, h):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE, bufsize=10**8)
    fs = w * h * 3
    while True:
        buf = p.stdout.read(fs)
        if len(buf) < fs:
            break
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    p.wait()


# ---------------- 逐帧 ----------------
def analyze_frames(video, fps):
    ys, xs = np.mgrid[0:AH, 0:AW].astype(np.float32)
    rx, ry = xs - AW / 2, ys - AH / 2
    rnorm = np.sqrt(rx**2 + ry**2) + 1e-6
    rows, prev_hsv, prev_gray = [], None, None
    for i, rgb in enumerate(frame_stream(video, AW, AH)):
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.int16)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        r = {"f": i, "t": round(i / fps, 4), "luma": round(float(gray.mean()), 2),
             "sat": round(float(hsv[..., 1].mean()), 2),
             "warm": round(float(rgb[..., 0].astype(np.int16).mean() - rgb[..., 2].astype(np.int16).mean()), 2)}
        if prev_hsv is not None:
            r["content"] = round(float(np.abs(hsv - prev_hsv).mean()), 2)
            if r["luma"] > 8 and prev_gray.mean() > 8:
                flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                fx, fy = flow[..., 0], flow[..., 1]
                r["dx"], r["dy"] = round(float(np.median(fx)), 3), round(float(np.median(fy)), 3)
                r["zoom"] = round(float(((fx * rx + fy * ry) / rnorm).mean()), 3)   # >0 推近，<0 拉远
                r["local"] = round(float(np.sqrt((fx - r["dx"])**2 + (fy - r["dy"])**2).mean()), 3)
        rows.append(r)
        prev_hsv, prev_gray = hsv, gray
    return rows


def detect_events(frames, fps):
    n = len(frames)
    c = np.array([f.get("content", 0) for f in frames])
    luma = np.array([f["luma"] for f in frames])
    cuts = []
    for i in range(1, n):
        neigh = np.concatenate([c[max(1, i - 12):max(1, i - 1)], c[i + 2:min(n, i + 13)]])
        med = np.median(neigh) if len(neigh) else 0
        dark = luma[i] < 30 and luma[i - 1] < 30
        if c[i] > max(14 if dark else 20, 3.5 * med + 6) and c[i] >= c[max(1, i - 2):min(n, i + 3)].max():
            cuts.append(i)
    flashes = []
    for i in range(2, n - 2):
        base = min(luma[i - 2], luma[min(n - 1, i + 6)])
        if luma[i] - base > 55 and luma[i] > 120 and luma[i] >= luma[i - 1] and (not flashes or i - flashes[-1]["end"] > 2):
            j = i
            while j < n - 1 and luma[j + 1] - base > 40:
                j += 1
            flashes.append({"t": round(i / fps, 3), "frames": j - i + 1, "end": j})
    soft, cutset, i = [], set(cuts), 1
    while i < n:
        if 2.5 < c[i] < 18:
            j = i
            while j < n and 2.5 < c[j] < 18 and j not in cutset:
                j += 1
            if j - i >= 8 and c[i:j].sum() > 45:
                soft.append({"t": round(i / fps, 3), "frames": j - i, "luma_min": round(float(luma[i:j].min()), 1)})
            i = j + 1
        else:
            i += 1
    blacks, i = [], 0
    while i < n:
        if luma[i] < 12:
            j = i
            while j < n and luma[j] < 12:
                j += 1
            if j - i >= 2:
                blacks.append({"t": round(i / fps, 3), "dur": round((j - i) / fps, 3)})
            i = j
        else:
            i += 1
    return cuts, flashes, soft, blacks


def motion_tags(fs):
    if len(fs) < 2:
        return ["固定/微动"], {}
    g = lambda k: np.array([f.get(k, 0) for f in fs])
    dx, dy, z, loc = g("dx"), g("dy"), g("zoom"), g("local")
    mdx, mdy, mz = float(dx.mean()), float(dy.mean()), float(z.mean())
    jit = float(np.hypot(dx.std(), dy.std())) if len(fs) > 3 else 0.0
    tags = []
    if mz > 0.06: tags.append("推近")
    elif mz < -0.06: tags.append("拉远")
    if abs(mdx) > 0.18: tags.append("横移/摇" + ("→" if mdx < 0 else "←"))
    if abs(mdy) > 0.15: tags.append("升降/俯仰" + ("↑" if mdy > 0 else "↓"))
    if jit > 0.45: tags.append("手持/晃")
    if float(loc.mean()) > 1.2: tags.append("主体强运动")
    return tags or ["固定/微动"], {"dx": round(mdx, 3), "dy": round(mdy, 3), "zoom": round(mz, 3), "jitter": round(jit, 3)}


# ---------------- 音频 ----------------
def analyze_audio(video):
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", video, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"])
    y = np.frombuffer(raw, np.float32)
    f, t, Z = stft(y, fs=SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
    S = np.abs(Z)
    dt = HOP / SR
    db = 20 * np.log10(np.sqrt(np.mean(S**2, axis=0) + 1e-12) + 1e-9)
    db -= db.max()
    norm = lambda x: (x - uniform_filter1d(x, int(3 / dt))) / ((x - uniform_filter1d(x, int(3 / dt))).std() + 1e-9)
    flux = norm(np.concatenate([[0], np.maximum(0, np.diff(np.log1p(100 * S), axis=1)).sum(axis=0)]))
    lowflux = norm(np.concatenate([[0], np.maximum(0, np.diff(np.log1p(100 * S[f < 150]), axis=1)).sum(axis=0)]))
    pk, _ = find_peaks(flux, height=1.5, distance=int(0.08 / dt))
    onsets = [{"t": round(float(t[i]), 3), "s": round(float(flux[i]), 2)} for i in pk]
    pk2, _ = find_peaks(lowflux, height=3.0, distance=int(0.25 / dt))
    hits = []
    for i in pk2:
        pre = db[max(0, i - int(0.3 / dt)):i].mean() if i > 5 else db[i]
        hits.append({"t": round(float(t[i]), 3), "s": round(float(lowflux[i]), 2),
                     "jump_db": round(float(db[i:i + int(0.15 / dt)].max() - pre), 1)})
    quiet = (db < maximum_filter1d(db, int(3 / dt)) - 28) | (db < -50)
    sil, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * dt >= 0.1:
                sil.append({"start": round(float(t[i]), 3), "dur": round((j - i) * dt, 3)})
            i = j
        else:
            i += 1
    step, curve = int(0.5 / dt), []
    low = S[f < 150].sum(axis=0)
    for k in range(0, len(db), step):
        sl = slice(k, k + step)
        curve.append({"t": round(k * dt, 2), "db": round(float(db[sl].mean()), 1), "peak": round(float(db[sl].max()), 1),
                      "low": round(float(low[sl].sum() / (S[:, sl].sum() + 1e-9)), 3)})
    tempo, win, oenv = [], int(8 / dt), np.maximum(flux, 0)
    for k in range(0, len(oenv) - win, int(4 / dt)):
        seg = oenv[k:k + win] - oenv[k:k + win].mean()
        ac = np.correlate(seg, seg, "full")[win - 1:]
        lo, hi = int(60 / 180 / dt), int(60 / 60 / dt)
        if ac[0] > 0:
            lag = lo + int(np.argmax(ac[lo:hi]))
            tempo.append({"t": round(k * dt, 1), "bpm": round(60 / (lag * dt), 1), "conf": round(float(ac[lag] / ac[0]), 2)})
    return {"onsets": onsets, "hits": hits, "silences": sil, "curve": curve, "tempo": tempo}


# ---------------- 汇总 ----------------
def build_shots(meta, frames, au):
    fps, n, T = meta["fps"], len(frames), meta["duration"]
    cuts, flashes, soft, blacks = detect_events(frames, fps)
    bounds = [0] + cuts + [n]
    on_t = np.array([o["t"] for o in au["onsets"]]) if au["onsets"] else np.array([1e9])
    hit_t = np.array([h["t"] for h in au["hits"]]) if au["hits"] else np.array([1e9])
    shots = []
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        fs = frames[a:b]
        tags, mv = motion_tags(fs[1:])
        t0 = a / fps
        shots.append({"i": k, "start_f": a, "end_f": b, "t": round(t0, 3), "dur": round((b - a) / fps, 3),
                      "luma": round(float(np.mean([f["luma"] for f in fs])), 1),
                      "warm": round(float(np.mean([f["warm"] for f in fs])), 1),
                      "sat": round(float(np.mean([f["sat"] for f in fs])), 1),
                      "motion": tags, "mv": mv,
                      "onset_dist": round(float(np.min(np.abs(on_t - t0))), 3),
                      "hit_dist": round(float(np.min(np.abs(hit_t - t0))), 3)})
    durs = np.array([s["dur"] for s in shots])
    tol = 2.5 / fps
    rest = shots[1:]
    rnd = np.random.default_rng(0).uniform(0, T, 4000)
    acts = []
    for a, b in [(0, .25), (.25, .65), (.65, .9), (.9, 1.0)]:
        sel = [s for s in shots if a * T <= s["t"] < b * T and s["luma"] > 15]
        if sel:
            d = np.array([s["dur"] for s in sel])
            acts.append({"range": f"{int(a*100)}-{int(b*100)}%", "shots": len(sel), "asl": round(float(d.mean()), 2),
                         "median": round(float(np.median(d)), 2), "under1s": round(float((d < 1).mean()), 2),
                         "handheld": round(float(np.mean([any("手持" in m for m in s["motion"]) for s in sel])), 2),
                         "warm": round(float(np.mean([s["warm"] for s in sel])), 1)})
    motion = {}
    for s in shots:
        for m in s["motion"]:
            key = m.rstrip("←→↑↓")
            motion[key] = motion.get(key, 0) + 1
    summary = {
        "meta": meta, "shots": len(shots), "asl": round(float(durs.mean()), 2), "median": round(float(np.median(durs)), 2),
        "under1s": round(float((durs < 1).mean()), 2),
        "cut_on_onset": round(float(np.mean([s["onset_dist"] <= tol for s in rest])), 3) if rest else 0,
        "onset_random_baseline": round(float(np.mean([np.min(np.abs(on_t - r)) <= tol for r in rnd])), 3),
        "hits_with_cut": round(float(np.mean([np.min(np.abs(np.array([s["t"] for s in rest]) - h)) <= tol for h in hit_t])), 3)
        if rest and au["hits"] else 0,
        "acts": acts, "motion_pct": {k: round(v / len(shots), 2) for k, v in motion.items()},
        "flashes": flashes, "soft_transitions": soft, "blacks": blacks, "silences": au["silences"],
        "big_hits": [h for h in au["hits"] if h["jump_db"] >= 8],
    }
    return shots, summary


def write_summary(path, s, speech):
    L = [f"文件: {s['meta']['video']}", f"时长 {s['meta']['duration']:.1f}s  {s['meta']['fps']:.3f}fps  "
         f"{s['meta']['src_w']}x{s['meta']['src_h']}",
         f"镜头 {s['shots']}  平均 {s['asl']}s  中位 {s['median']}s  <1s 占 {s['under1s']:.0%}",
         f"切点落在起音±2帧: {s['cut_on_onset']:.0%}（随机基线 {s['onset_random_baseline']:.0%}）  大冲击伴随切点: {s['hits_with_cut']:.0%}",
         "分段: " + " | ".join(f"{a['range']} {a['shots']}镜 ASL{a['asl']} <1s{a['under1s']:.0%} 手持{a['handheld']:.0%} 色温{a['warm']:+}" for a in s["acts"]),
         "运镜: " + "  ".join(f"{k}{v:.0%}" for k, v in sorted(s["motion_pct"].items(), key=lambda x: -x[1])),
         "闪白: " + ", ".join(f"{f['t']}s/{f['frames']}帧" for f in s["flashes"]),
         "黑场: " + ", ".join(f"{b['t']}s/{b['dur']}s" for b in s["blacks"]),
         "软转场(叠化/渐变候选): " + ", ".join(f"{x['t']}s/{x['frames']}帧" for x in s["soft_transitions"]),
         "静音断点: " + ", ".join(f"{x['start']}s/{x['dur']}s" for x in s["silences"]),
         "大冲击(≥8dB跃升): " + ", ".join(f"{h['t']}s/+{h['jump_db']}dB" for h in s["big_hits"])]
    if speech:
        L.append("台词:")
        L += [f"  {x['start']:6.1f}-{x['end']:6.1f} {x['text']}" for x in speech]
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L[:10]))


def contact_sheets(video, shots, fps, out_dir, every=None, nframes=0):
    TW, TH, COLS, ROWS = 384, 216, 4, 5
    items = []
    if every:
        items = [(f, f"{f / fps:6.2f}s") for f in range(0, nframes, max(1, int(every * fps)))]
    else:
        for s in shots:
            a, b = s["start_f"], s["end_f"] - 1
            lab = f"#{s['i']} {s['t']:.2f}s {s['dur']:.2f}s {'/'.join(s['motion'])}"
            items += [(a + 1, lab + " [首]"), ((a + b) // 2, "  中"), (b - 1, "  尾")] if s["dur"] >= 2.5 else [((a + b) // 2, lab)]
    want = {f for f, _ in items}
    grabbed = {}
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", video, "-vf",
                          f"scale={TW}:{TH}:force_original_aspect_ratio=decrease,pad={TW}:{TH}:(ow-iw)/2:(oh-ih)/2",
                          "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], stdout=subprocess.PIPE, bufsize=10**8)
    fs, k = TW * TH * 3, 0
    while True:
        buf = p.stdout.read(fs)
        if len(buf) < fs:
            break
        if k in want:
            grabbed[k] = Image.frombytes("RGB", (TW, TH), buf)
        k += 1
    p.wait()
    os.makedirs(os.path.join(out_dir, "sheets"), exist_ok=True)
    F, per = font(15), COLS * ROWS
    for pi in range(0, len(items), per):
        sheet = Image.new("RGB", (COLS * TW, ROWS * (TH + 22)), (20, 20, 20))
        dr = ImageDraw.Draw(sheet)
        for j, (f, lab) in enumerate(items[pi:pi + per]):
            x, y = (j % COLS) * TW, (j // COLS) * (TH + 22)
            if f in grabbed:
                sheet.paste(grabbed[f], (x, y + 22))
            dr.text((x + 4, y + 2), lab, fill=(255, 230, 120), font=F)
        sheet.save(os.path.join(out_dir, "sheets", f"sheet_{pi // per:02d}.jpg"), quality=82)


def timeline_png(path, shots, summary, au, speech):
    W, H = 2400, 520
    T = summary["meta"]["duration"]
    X = lambda t: int(40 + (W - 60) * t / T)
    F = font(14)
    img = Image.new("RGB", (W, H), (18, 18, 18)); dr = ImageDraw.Draw(img)
    for s in range(0, int(T) + 1, 5):
        dr.line([(X(s), 20), (X(s), H - 20)], fill=(70, 70, 70) if s % 10 == 0 else (45, 45, 45))
        if s % 10 == 0:
            dr.text((X(s) + 2, H - 18), f"{s}s", fill=(160, 160, 160), font=F)
    dr.line([(X(c["t"]), 40 + int(-max(c["peak"], -40) / 40 * 160)) for c in au["curve"]], fill=(40, 90, 120))
    dr.line([(X(c["t"]), 40 + int(-max(c["db"], -40) / 40 * 160)) for c in au["curve"]], fill=(90, 200, 255), width=2)
    dr.line([(X(c["t"]), 200 - int(c["low"] * 160)) for c in au["curve"]], fill=(255, 120, 60))
    dr.text((44, 22), "响度(蓝,0~-40dB) 低频占比(橙)", fill=(90, 200, 255), font=F)
    for s in au["silences"]:
        dr.rectangle([X(s["start"]), 210, max(X(s["start"] + s["dur"]), X(s["start"]) + 2), 230], fill=(220, 220, 220))
    dr.text((44, 212), "静音断点", fill=(220, 220, 220), font=F)
    for h in au["hits"]:
        dr.line([(X(h["t"]), 240), (X(h["t"]), 240 + int(min(h["s"], 12) * 3))], fill=(255, 80, 80), width=2)
    dr.text((44, 242), "低频冲击", fill=(255, 80, 80), font=F)
    for s in shots:
        x0, x1 = X(s["t"]), X(s["t"] + s["dur"])
        dr.rectangle([x0, 400 - int(min(s["dur"], 4) / 4 * 120), max(x1 - 1, x0), 400],
                     fill=(255, 220, 90) if s["luma"] > 15 else (90, 90, 90))
    dr.text((44, 282), "镜头时长(黄,0~4s;灰=黑场/暗)", fill=(255, 220, 90), font=F)
    for s in speech:
        dr.rectangle([X(s["start"]), 410, X(s["end"]), 425], fill=(120, 230, 120))
    dr.text((44, 428), "台词(绿)", fill=(120, 230, 120), font=F)
    for f in summary["flashes"]:
        dr.rectangle([X(f["t"]), 445, X(f["t"]) + 3, 460], fill=(255, 255, 255))
    for b in summary["blacks"]:
        dr.rectangle([X(b["t"]), 465, X(b["t"] + b["dur"]), 480], fill=(110, 110, 160))
    dr.text((44, 482), "闪白(白) 黑场(紫)", fill=(170, 170, 220), font=F)
    img.save(path)


def transcribe(video):
    from faster_whisper import WhisperModel
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", video, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"])
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, info = model.transcribe(np.frombuffer(raw, np.float32).copy(), vad_filter=True, beam_size=5)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--transcribe", action="store_true")
    ap.add_argument("--every", type=float, default=None)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    fps, dur, sw, sh = probe(a.video)
    print(f"[1/5] 逐帧分析 {os.path.basename(a.video)} ({dur:.1f}s @ {fps:.3f}fps)…")
    frames = analyze_frames(a.video, fps)
    meta = {"video": os.path.basename(a.video), "fps": fps, "duration": dur, "frames": len(frames), "src_w": sw, "src_h": sh}
    json.dump({"meta": meta, "frames": frames}, open(os.path.join(a.out, "frames.json"), "w", encoding="utf-8"))
    print("[2/5] 音频分析…")
    au = analyze_audio(a.video)
    json.dump(au, open(os.path.join(a.out, "audio.json"), "w", encoding="utf-8"))
    speech = []
    if a.transcribe:
        print("[2b] 转写台词…")
        try:
            speech = transcribe(a.video)
            json.dump(speech, open(os.path.join(a.out, "speech.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except ImportError:
            print("  未安装 faster-whisper，跳过（pip install faster-whisper）")
    print("[3/5] 切分镜头…")
    shots, summary = build_shots(meta, frames, au)
    json.dump({"summary": summary, "shots": shots}, open(os.path.join(a.out, "shots.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    write_summary(os.path.join(a.out, "summary.txt"), summary, speech)
    print("[4/5] 接触表…")
    contact_sheets(a.video, shots, fps, a.out, a.every, len(frames))
    print("[5/5] 时间线图…")
    timeline_png(os.path.join(a.out, "timeline.png"), shots, summary, au, speech)
    print("完成 →", a.out)


if __name__ == "__main__":
    main()
