"""给配乐打剪辑点：节拍、小节、冲击、静音、段落变化。导出可导入剪辑软件的标记。

用法:
  python music_markers.py 配乐.wav --out 输出目录 [--fps 24] [--bpm 120] [--start-tc 01:00:00:00]
                          [--beats-per-bar 4] [--min-hit-db 6]

  --bpm        已知速度时填上（Suno 风格提示词里写了 BPM 就填），只在其 ±8% 内搜
  --start-tc   DaVinci 时间线起始时间码，默认 01:00:00:00

输出:
  markers.csv          time, timecode, type(bar/beat/hit/silence/section), strength, label
  resolve_markers.edl  DaVinci：时间线 → 导入 → 时间线标记（EDL）。只放 bar/hit/silence/section
  ae_markers.jsx       After Effects：选中合成后 文件 → 脚本 → 运行脚本文件，生成带标记的空对象层
  markers.json         给 Remotion / 自写脚本用
  markers.png          波形 + 标记总览
依赖: ffmpeg；pip install numpy scipy pillow
"""
import argparse, json, os, subprocess, sys
import numpy as np
from scipy.signal import stft, find_peaks
from scipy.ndimage import uniform_filter1d, maximum_filter1d
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")
SR, NFFT, HOP = 22050, 2048, 256
DT = HOP / SR


def load(path):
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"])
    return np.frombuffer(raw, np.float32)


def features(y):
    f, t, Z = stft(y, fs=SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
    S = np.abs(Z)
    logS = np.log1p(100 * S)
    onset = np.concatenate([[0], np.maximum(0, np.diff(logS, axis=1)).sum(axis=0)])
    low = np.concatenate([[0], np.maximum(0, np.diff(logS[f < 150], axis=1)).sum(axis=0)])
    db = 20 * np.log10(np.sqrt(np.mean(S**2, axis=0) + 1e-12) + 1e-9)
    db -= db.max()
    return t, onset, low, db


def local_norm(x, sec=3):
    m = uniform_filter1d(x, max(1, int(sec / DT)))
    d = x - m
    return d / (d.std() + 1e-9)


def estimate_period(env, bpm_hint=None):
    """全曲自相关估计拍长（帧）。"""
    e = env - env.mean()
    ac = np.correlate(e, e, "full")[len(e) - 1:]
    lo, hi = (60 / (bpm_hint * 1.08), 60 / (bpm_hint * 0.92)) if bpm_hint else (60 / 170, 60 / 70)
    lo_f, hi_f = int(lo / DT), int(hi / DT) + 1
    lag = lo_f + int(np.argmax(ac[lo_f:hi_f]))
    # 抛物线插值得到亚帧精度
    if 1 <= lag < len(ac) - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        lag = lag + 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
    return lag


def track_beats(env, period, tightness=100.0):
    """Ellis 2007 动态规划节拍跟踪。"""
    n = len(env)
    score = env.copy()
    back = -np.ones(n, dtype=int)
    p = period
    lo, hi = int(round(p / 2)), int(round(p * 2))
    offs = np.arange(-hi, -lo + 1)
    pen = -tightness * (np.log(-offs / p)) ** 2
    for i in range(n):
        cand = i + offs
        ok = cand >= 0
        if not ok.any():
            continue
        vals = score[cand[ok]] + pen[ok]
        j = int(np.argmax(vals))
        if vals[j] > 0:
            score[i] = env[i] + vals[j]
            back[i] = cand[ok][j]
    # 从最后一个拍长内的最高分回溯
    tail = np.arange(max(0, n - int(p * 1.5)), n)
    i = int(tail[np.argmax(score[tail])])
    beats = [i]
    while back[i] >= 0:
        i = back[i]
        beats.append(i)
    return np.array(beats[::-1])


def tc(sec, fps, start_frames=0):
    fr = int(round(sec * fps)) + start_frames
    fpsi = int(round(fps))
    return f"{fr // (3600 * fpsi):02d}:{fr // (60 * fpsi) % 60:02d}:{fr // fpsi % 60:02d}:{fr % fpsi:02d}"


def tc_to_frames(s, fps):
    h, m, sec, fr = [int(x) for x in s.split(":")]
    return ((h * 60 + m) * 60 + sec) * int(round(fps)) + fr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=24)
    ap.add_argument("--bpm", type=float, default=None)
    ap.add_argument("--start-tc", default="01:00:00:00")
    ap.add_argument("--beats-per-bar", type=int, default=4)
    ap.add_argument("--min-hit-db", type=float, default=6)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    y = load(a.audio)
    T = len(y) / SR
    t, onset, low, db = features(y)
    env = np.maximum(local_norm(onset), 0)
    period = estimate_period(env, a.bpm)
    bpm = 60 / (period * DT)
    beats_f = track_beats(env, period)
    # 只保留落在有声段的拍
    loud = db > -45
    beats_f = beats_f[loud[beats_f]]
    beats = t[beats_f]
    # 小节首拍：哪个相位上低频起音最强
    lown = np.maximum(local_norm(low), 0)
    bpb = a.beats_per_bar
    phase = int(np.argmax([lown[beats_f[k::bpb]].sum() for k in range(bpb)])) if len(beats_f) >= bpb else 0
    bars = beats[phase::bpb]

    # 冲击
    ln = local_norm(low)
    pk, _ = find_peaks(ln, height=3.0, distance=int(0.25 / DT))
    hits = []
    for i in pk:
        pre = db[max(0, i - int(0.3 / DT)):i].mean() if i > 5 else db[i]
        jump = float(db[i:i + int(0.15 / DT)].max() - pre)
        if jump >= a.min_hit_db:
            hits.append((float(t[i]), round(jump, 1)))
    # 静音 / drop
    quiet = (db < maximum_filter1d(db, int(3 / DT)) - 28) | (db < -50)
    sils, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * DT >= 0.15 and 0.5 < t[i] < T - 1:
                sils.append((float(t[i]), round((j - i) * DT, 2)))
            i = j
        else:
            i += 1
    # 段落变化：2 秒平滑响度的大幅变化
    sm = uniform_filter1d(db, int(2 / DT))
    step = int(1 / DT)
    secs, last = [], -99
    for k in range(step * 2, len(sm) - step * 2, step // 4):
        d = sm[k + step] - sm[k - step]
        if abs(d) >= 6 and t[k] - last > 4:
            secs.append((float(t[k]), round(float(d), 1)))
            last = t[k]

    rows = [(float(b), "beat", 1, "") for b in beats]
    rows += [(float(b), "bar", 2, f"小节{n + 1}") for n, b in enumerate(bars)]
    rows += [(h, "hit", jd, f"冲击+{jd}dB") for h, jd in hits]
    rows += [(s, "silence", d, f"静音{d}s") for s, d in sils]
    rows += [(s, "section", d, ("升" if d > 0 else "降") + f"{abs(d)}dB") for s, d in secs]
    rows.sort(key=lambda r: (r[0], r[1]))

    start = tc_to_frames(a.start_tc, a.fps)
    with open(os.path.join(a.out, "markers.csv"), "w", encoding="utf-8-sig") as fp:
        fp.write("time,timecode,type,strength,label\n")
        for r in rows:
            fp.write(f"{r[0]:.3f},{tc(r[0], a.fps)},{r[1]},{r[2]},{r[3]}\n")
    json.dump({"duration": T, "bpm": round(bpm, 2), "fps": a.fps, "beats": [round(float(b), 3) for b in beats],
               "bars": [round(float(b), 3) for b in bars], "hits": hits, "silences": sils, "sections": secs},
              open(os.path.join(a.out, "markers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # DaVinci Resolve 标记 EDL
    colors = {"bar": "ResolveColorBlue", "hit": "ResolveColorRed", "silence": "ResolveColorYellow", "section": "ResolveColorGreen"}
    with open(os.path.join(a.out, "resolve_markers.edl"), "w", encoding="utf-8") as fp:
        fp.write("TITLE: Music Markers\nFCM: NON-DROP FRAME\n\n")
        n = 0
        for r in rows:
            if r[1] == "beat":
                continue
            n += 1
            s = tc(r[0], a.fps, start)
            e = tc(r[0] + 1 / a.fps, a.fps, start)
            fp.write(f"{n:03d}  001      V     C        {s} {e} {s} {e}  \n |C:{colors[r[1]]} |M:{r[3] or r[1]} |D:1\n\n")

    # After Effects 脚本
    data = ",".join(f'[{r[0]:.3f},"{r[3] or r[1]}"]' for r in rows if r[1] != "beat")
    beats_js = ",".join(f"{b:.3f}" for b in beats)
    jsx = f"""// 由 music_markers.py 生成。先在 AE 里选中目标合成，再运行本脚本。
(function () {{
  var comp = app.project.activeItem;
  if (!(comp && comp instanceof CompItem)) {{ alert("请先选中一个合成"); return; }}
  app.beginUndoGroup("Music Markers");
  function addLayer(name, items, labelFn) {{
    var l = comp.layers.addNull(comp.duration);
    l.name = name; l.enabled = false; l.startTime = 0;
    var m = l.property("ADBE Marker");
    for (var i = 0; i < items.length; i++) {{
      var it = items[i];
      if (labelFn(it)[0] <= comp.duration) m.setValueAtTime(labelFn(it)[0], new MarkerValue(labelFn(it)[1]));
    }}
  }}
  addLayer("MUSIC_BEATS", [{beats_js}], function (x) {{ return [x, ""]; }});
  addLayer("MUSIC_MARKERS", [{data}], function (x) {{ return [x[0], x[1]]; }});
  app.endUndoGroup();
}})();
"""
    open(os.path.join(a.out, "ae_markers.jsx"), "w", encoding="utf-8").write(jsx)

    # 总览图
    W, H = 2400, 360
    X = lambda s: int(30 + (W - 50) * s / T)
    img = Image.new("RGB", (W, H), (18, 18, 18)); dr = ImageDraw.Draw(img)
    try:
        F = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
    except OSError:
        F = ImageFont.load_default()
    env_db = maximum_filter1d(db, int(0.05 / DT))
    pts = [(X(t[k]), 180 - int((max(env_db[k], -50) + 50) / 50 * 140)) for k in range(0, len(t), 4)]
    dr.line(pts, fill=(90, 200, 255), width=1)
    for b in beats:
        dr.line([(X(b), 200), (X(b), 215)], fill=(110, 110, 110))
    for n, b in enumerate(bars):
        dr.line([(X(b), 195), (X(b), 230)], fill=(80, 140, 255), width=2)
        if n % 4 == 0:
            dr.text((X(b) + 2, 232), str(n + 1), fill=(80, 140, 255), font=F)
    for h, jd in hits:
        dr.line([(X(h), 250), (X(h), 250 + int(min(jd, 20) * 3))], fill=(255, 80, 80), width=3)
    for s, d in sils:
        dr.rectangle([X(s), 315, max(X(s + d), X(s) + 2), 330], fill=(240, 220, 80))
    for s, d in secs:
        dr.text((X(s), 20), "▲" if d > 0 else "▼", fill=(120, 230, 120), font=F)
    for s in range(0, int(T) + 1, 10):
        dr.text((X(s), H - 20), f"{s}s", fill=(150, 150, 150), font=F)
    dr.text((30, 2), f"BPM≈{bpm:.1f}  拍(灰) 小节(蓝,每4小节标号) 冲击(红) 静音(黄) 段落升降(绿)", fill=(220, 220, 220), font=F)
    img.save(os.path.join(a.out, "markers.png"))

    print(f"时长 {T:.1f}s  BPM≈{bpm:.1f}（一拍 {60 / bpm:.3f}s = {60 / bpm * a.fps:.1f} 帧@{a.fps:g}fps）")
    print(f"拍 {len(beats)}  小节 {len(bars)}  冲击 {len(hits)}  静音 {len(sils)}  段落变化 {len(secs)}")
    print("冲击:", ", ".join(f"{h:.2f}s(+{jd})" for h, jd in hits[:20]))
    print("静音:", ", ".join(f"{s:.2f}s({d}s)" for s, d in sils))
    print("输出 →", a.out)


if __name__ == "__main__":
    main()
