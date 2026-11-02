"""fx.py —— 预告片剪辑小工具：不用 AI 视频，用代码做的那部分活（制作级别 L1 / L2）。

  sheet     抽帧检查图：均匀抽 N 帧，或把一段时间里的每一帧都排出来（查闪白、频闪、口吃剪落在哪一帧）
  montage   点名蒙太奇 / 闪切：一组图片或视频按固定帧数、越切越快（6:3）或按拍拼起来；--ghost 每张快推 + 残影
  strobe    频闪：亮 N 帧、暗 M 帧交替（频闪蒙太奇）
  punch     冲推：在指定时间点猛地推近，带动态模糊
  whip      甩镜转场：A 结尾甩出、B 开头甩入
  glitch    画面故障：指定时间点几帧红蓝错位 + 横条撕裂
  stutter   口吃剪：同一镜头切成几帧一片的碎片，一片片重复着往前跳
  cutaudio  截一段音频：给对白条目当参考音频，或截配乐片段

用法示例:
  python fx.py sheet 粗剪_v4.mp4 --out 检查.jpg
  python fx.py sheet 粗剪_v4.mp4 --every-frame --start 31.0 --end 31.6 --out 逐帧.jpg
  python fx.py montage --dir 点名肖像 --accel 6:3 --out L1_088_点名.mp4
  python fx.py montage A.png B.png C.png D.png --frames 6 --ghost --centers "0.5,0.35" --out L1_090_点名重影.mp4
  python fx.py strobe 爆发.mp4 --on 6 --off 3 --out L1_061_频闪.mp4
  python fx.py punch 拔剑.mp4 --from 1.2 --dur 1 --at 0.25 --out L1_072_冲推.mp4
  python fx.py whip --a 山门.mp4 --b 法阵.mp4 --out L1_050_甩镜.mp4
  python fx.py glitch 对讲机.mp4 --at 0.4 --frames 4 --out L1_023_故障.mp4
  python fx.py stutter 侧脸.mp4 --from 1.2 --piece 5 --step 2 --count 6 --out L1_057_口吃剪.mp4
  python fx.py cutaudio 台词_沈青.wav --start 12.4 --dur 5 --lead 1 --out 参考音频_沈青.wav

时间写法（所有时间参数都认）: 1.5 = 1.5 秒；12f / 12帧 = 12 帧；2拍 / 2b / 半拍 = 按拍（要同时给 --bpm）
做出来的片段放进分镜表"素材"列，命名建议 L1_镜号_说明.mp4；片段比分镜表里的时长多做 1–2 帧，不然最后一帧会定格。
只会新建 --out 指定的文件；--out 和输入是同一个文件时拒绝运行；--out 已经存在会被覆盖。
依赖: ffmpeg / ffprobe；pip install numpy opencv-python pillow
"""
import argparse, json, math, os, re, subprocess, sys
import numpy as np
import cv2
from PIL import Image, ImageOps

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
PUNCH_SCALE = 1.15          # animatic.py 里"冲推"推到多大


# ---------------------------------------------------------------- 基础工具
def die(msg):
    sys.exit("出错了：" + msg)


def parse_time(s, bpm=None, fps=24.0, what="时间"):
    t = str(s).strip().lower()
    if t in ("半拍", "半b"):
        t = "0.5拍"
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(f|帧|b|拍|s|秒)?", t)
    if not m:
        die(f"{what}「{s}」看不懂，写成 1.5（秒）、12f（帧）或 2拍（要给 --bpm）")
    v, u = float(m.group(1)), m.group(2) or "s"
    if u in ("f", "帧"):
        return v / fps
    if u in ("b", "拍"):
        if not bpm:
            die(f"{what}「{s}」是按拍写的，要同时给 --bpm")
        return v * 60.0 / bpm
    return v


def parse_times(s, bpm, fps, what="时间点"):
    return [parse_time(x, bpm, fps, what) for x in re.split(r"[,，]", str(s)) if x.strip()]


def parse_size(s):
    try:
        w, h = (int(x) for x in s.lower().replace("×", "x").split("x"))
        return w // 2 * 2, h // 2 * 2
    except Exception:
        die(f"画面大小「{s}」写法不对，写成 1920x1080")


def probe(path):
    """视频的宽、高、帧率、时长。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", path], capture_output=True)
    try:
        j = json.loads(r.stdout.decode("utf-8", "ignore"))
    except ValueError:
        j = {}
    st = (j.get("streams") or [{}])[0]
    if not st.get("width"):
        die(f"读不了这个视频：{path}")
    num, den = (st.get("r_frame_rate") or "24/1").split("/")
    fps = float(num) / float(den) if float(den) else 24.0
    return {"w": int(st["width"]) // 2 * 2, "h": int(st["height"]) // 2 * 2, "fps": fps,
            "dur": float((j.get("format") or {}).get("duration") or 0)}


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True).stdout.strip()
    return bool(out)


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXT


def check_in(path):
    if not os.path.exists(path):
        die(f"找不到文件：{path}")
    if os.path.splitext(path)[1].lower() not in IMAGE_EXT | VIDEO_EXT:
        die(f"不支持的素材类型：{path}")


def check_out(out, *inputs):
    for i in inputs:
        if i and os.path.abspath(out).lower() == os.path.abspath(i).lower():
            die(f"输出和输入是同一个文件（{out}），会把原素材毁掉，换一个 --out 名字")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)


def load_image(path):
    """读图片（支持中文路径、手机照片自动转正；透明的地方当黑色）。"""
    try:
        im = ImageOps.exif_transpose(Image.open(path))
    except Exception:
        die(f"读不了图片：{path}")
    if im.mode in ("RGBA", "LA", "P") and (im.mode != "P" or "transparency" in im.info):
        im = Image.alpha_composite(Image.new("RGBA", im.size, (0, 0, 0, 255)), im.convert("RGBA"))
    return np.asarray(im.convert("RGB"))


def image_size(path):
    h, w = load_image(path).shape[:2]
    return w // 2 * 2, h // 2 * 2


def cover(img, W, H):
    """等比缩放后居中裁切，铺满 W×H。"""
    h, w = img.shape[:2]
    if (w, h) == (W, H):
        return img
    s = max(W / w, H / h)
    nw, nh = max(W, round(w * s)), max(H, round(h * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    x, y = (nw - W) // 2, (nh - H) // 2
    return r[y:y + H, x:x + W]


def ease(u, kind="inout"):
    u = min(max(u, 0.0), 1.0)
    if kind == "in":
        return u * u
    if kind == "out":
        return 1 - (1 - u) * (1 - u)
    return 0.5 - 0.5 * math.cos(math.pi * u)


def zoom(frame, z, cx=0.5, cy=0.5):
    """以 (cx, cy)（画面比例）为中心放大 z 倍。"""
    H, W = frame.shape[:2]
    px, py = cx * W, cy * H
    M = np.float32([[z, 0, px - z * px], [0, z, py - z * py]])
    return cv2.warpAffine(frame, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


class Frames:
    """从视频（或图片）第 start 秒起读 n 帧，统一成 W×H 和 fps。视频不够长时用最后一帧补，frozen 记补了几帧。"""

    def __init__(self, path, start, n, W, H, fps):
        self.path, self.start, self.n, self.W, self.H, self.frozen = path, start, n, W, H, 0
        self.img, self.p = None, None
        if is_image(path):
            self.img = cover(load_image(path), W, H)
        else:
            vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1"
            self.p = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", path, "-vf", vf,
                                       "-frames:v", str(n), "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                      stdout=subprocess.PIPE, bufsize=10 ** 7)

    def __iter__(self):
        last, size = None, self.W * self.H * 3
        for _ in range(self.n):
            if self.img is not None:
                yield self.img
                continue
            buf = self.p.stdout.read(size) if self.p else b""
            if len(buf) == size:
                last = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3)
            else:
                self._close()
                if last is None:
                    die(f"读不到画面：{self.path} 第 {self.start:.2f} 秒以后没有画面")
                self.frozen += 1
            yield last
        self._close()

    def _close(self):
        if self.p:
            self.p.stdout.close()
            self.p.wait()
            self.p = None


def grab(path, t, W, H):
    """取视频第 t 秒的一帧。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-vf",
                        f"scale={W}:{H}", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True)
    if len(r.stdout) < W * H * 3:
        return np.zeros((H, W, 3), np.uint8)
    return np.frombuffer(r.stdout[:W * H * 3], np.uint8).reshape(H, W, 3)


class Writer:
    """把画面一帧帧交给 ffmpeg 编码成 mp4；可选带上一条声音（从 audio 文件第 audio_start 秒起）。"""

    def __init__(self, out, W, H, fps, audio=None, audio_start=0.0):
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", f"{fps}", "-i", "-"]
        if audio:
            cmd += ["-ss", f"{audio_start:.3f}", "-i", audio, "-map", "0:v", "-map", "1:a?", "-af", "apad", "-shortest",
                    "-c:a", "aac", "-b:a", "256k"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        try:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except FileNotFoundError:
            die("找不到 ffmpeg，先安装并确认命令行里能运行 ffmpeg")
        self.out, self.fps, self.n = out, fps, 0

    def write(self, f):
        try:
            self.p.stdin.write(np.ascontiguousarray(f, dtype=np.uint8).tobytes())
        except OSError:
            self.p.wait()
            die("ffmpeg 中途停了（上面几行英文是原因）")
        self.n += 1

    def close(self, quiet=False):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            die("ffmpeg 编码失败（上面几行英文是原因）")
        if not quiet:
            print(f"完成：{self.out}（{self.n} 帧，{self.n / self.fps:.2f} 秒）")


def report_frozen(path, frozen, fps):
    if frozen:
        print(f"注意：{os.path.basename(path)} 不够长，最后 {frozen} 帧（{frozen / fps:.2f} 秒）是定格")


# ---------------------------------------------------------------- 效果（也给 animatic.py 用）
def rgb_split(f, px):
    out = f.copy()
    out[..., 0] = np.roll(f[..., 0], px, axis=1)     # 红往右
    out[..., 2] = np.roll(f[..., 2], -px, axis=1)    # 蓝往左
    return out


def lighten(base, layer, alpha):
    """只叠比底图亮的地方（用"滤色"叠会把暗部抬灰，切点那帧像起雾）。"""
    return np.maximum(base, layer * alpha)


def glitch_frame(f, k, amount=0.02, seed=0):
    rng = np.random.default_rng(seed * 1000 + k)
    H, W = f.shape[:2]
    g = rgb_split(f, max(2, int(W * amount)))
    for _ in range(int(rng.integers(3, 8))):          # 横向撕裂条
        y = int(rng.integers(0, H - 10))
        hh = int(rng.integers(4, max(5, H // 12)))
        g[y:y + hh] = np.roll(g[y:y + hh], int(rng.integers(-W // 8, W // 8)), axis=1)
    return g


def punch_frame(f, j, ramp, z_from, z_to):
    """冲推第 j 帧：前 ramp 帧从 z_from 推到 z_to（叠几层不同倍数做动态模糊），之后停在 z_to。"""
    if j < ramp:
        u0, u1 = ease(j / ramp, "out"), ease((j + 1) / ramp, "out")
        subs = [zoom(f, z_from + (z_to - z_from) * (u0 + (u1 - u0) * i / 4)) for i in range(5)]
        return np.mean(subs, axis=0).astype(np.uint8)
    return zoom(f, z_to)


def head_effects(src, out, effects, n, W, H, fps):
    """animatic.py 用：在一镜开头加效果并保留声音。effects = [("black", N) 开头 N 帧黑 / ("glitch", N) 开头 N 帧故障，
    N=0 表示整镜 / ("punch", N) 开头 N 帧猛推近]。"""
    w = Writer(out, W, H, fps, src if has_audio(src) else None)
    for k, f in enumerate(Frames(src, 0, n, W, H, fps)):
        for kind, N in effects:
            if kind == "punch":
                f = punch_frame(f, k, max(1, N), 1.0, PUNCH_SCALE)
            elif kind == "glitch" and (N == 0 or k < N):
                f = glitch_frame(f, k)
            elif kind == "black" and k < N:
                f = np.zeros_like(f)
        w.write(f)
    w.close(quiet=True)


# ---------------------------------------------------------------- 处理单个素材的命令
def run_single(a, fn):
    """逐帧处理一个素材（视频或图片），保留原声音。fn(frame, k, fps) 返回新画面。"""
    check_in(a.input)
    check_out(a.out, a.input)
    img = is_image(a.input)
    info = None if img else probe(a.input)
    fps = a.fps or (24.0 if img else round(info["fps"], 3))
    W, H = parse_size(a.size) if a.size else (image_size(a.input) if img else (info["w"], info["h"]))
    start = parse_time(a.from_, a.bpm, fps, "--from")
    if a.dur:
        dur = parse_time(a.dur, a.bpm, fps, "--dur")
    elif img:
        die("输入是图片，要用 --dur 说明做几秒")
    else:
        dur = info["dur"] - start
    n = int(round(dur * fps))
    if n <= 0:
        die(f"--from {start:.2f} 秒已经超过了素材长度")
    a._n, a._fps = n, fps
    if getattr(a, "at", None):                          # 参数先查完再开始读写，出错时不留半截文件
        a._starts = at_frames(a, fps, n)
    if hasattr(a, "on"):
        a._s = parse_time(a.start, a.bpm, fps, "--start") * fps
        a._e = parse_time(a.end, a.bpm, fps, "--end") * fps if a.end else 1e18
    w = Writer(a.out, W, H, fps, None if img or not has_audio(a.input) else a.input, start)
    fr = Frames(a.input, start, n, W, H, fps)
    for k, f in enumerate(fr):
        w.write(fn(f, k, fps))
    w.close()
    report_frozen(a.input, fr.frozen, fps)


def at_frames(a, fps, n):
    starts = sorted(int(round(t * fps)) for t in parse_times(a.at, a.bpm, fps, "--at"))
    late = [s for s in starts if s >= n]
    if late:
        print(f"注意：有 {len(late)} 个时间点超过了素材长度 {n / fps:.2f} 秒，不会生效")
    return starts


def cmd_strobe(a):
    def fn(f, k, fps):
        if a._s <= k < a._e and (k - a._s) % (a.on + a.off) >= a.on:
            return (f.astype(np.float32) * a.dark).astype(np.uint8)
        return f
    run_single(a, fn)


def cmd_glitch(a):
    run_single(a, lambda f, k, fps: glitch_frame(f, k, a.amount, a.seed) if any(s <= k < s + a.frames for s in a._starts) else f)


def cmd_punch(a):
    def fn(f, k, fps):
        prev = [s for s in a._starts if s <= k]
        if not prev:
            return f
        if a.reset:
            z_from, z_to = 1.0, a.scale
        else:                                           # 默认一次比一次近
            z_from = 1.0 + (a.scale - 1.0) * (len(prev) - 1)
            z_to = z_from + (a.scale - 1.0)
        return punch_frame(f, k - prev[-1], a.ramp, z_from, z_to)
    run_single(a, fn)


# ---------------------------------------------------------------- 其他命令
def cmd_sheet(a):
    check_in(a.input)
    if is_image(a.input):
        die("sheet 只用于视频")
    check_out(a.out, a.input)
    info = probe(a.input)
    fps = info["fps"]
    tw = a.thumb // 2 * 2
    th = max(2, int(round(tw * info["h"] / max(1, info["w"]))) // 2 * 2)
    if a.every_frame:
        s = parse_time(a.start, a.bpm, fps, "--start")
        e = parse_time(a.end, a.bpm, fps, "--end") if a.end else info["dur"]
        i0, i1 = int(round(s * fps)), int(round(e * fps))
        n = i1 - i0
        if n <= 0:
            die("--end 要比 --start 晚")
        if n > 240:
            die(f"要排 {n} 帧，太多了（最多 240 帧），把 --start / --end 缩短")
        fr = Frames(a.input, i0 / fps, n, tw, th, fps)
        got = list(fr)
        thumbs = got[:len(got) - fr.frozen] or got[:1]          # 超出结尾补的定格帧不排
        ids = list(range(i0, i0 + len(thumbs)))
    else:
        ts = [info["dur"] * (i + 0.5) / a.count for i in range(a.count)]
        thumbs = [grab(a.input, t, tw, th) for t in ts]
        ids = [int(round(t * fps)) for t in ts]
    cols = max(1, a.cols)
    rows = (len(thumbs) + cols - 1) // cols
    head = 20
    sheet = np.full((rows * (th + head), cols * tw, 3), 20, np.uint8)
    for i, (im, fi) in enumerate(zip(thumbs, ids)):
        r, c = divmod(i, cols)
        y = r * (th + head)
        sheet[y + head:y + head + th, c * tw:(c + 1) * tw] = im
        cv2.putText(sheet, f"#{fi} {fi / fps:.2f}s", (c * tw + 4, y + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 220, 80), 1, cv2.LINE_AA)
    Image.fromarray(sheet).save(a.out)
    print(f"完成：{a.out}（{len(thumbs)} 格，帧号按素材自己的帧率 {fps:.3f}）")


def parse_centers(s, n):
    if not s:
        return [(0.5, 0.5)] * n
    try:
        pts = [tuple(float(x) for x in p.split(",")) for p in re.split(r"[;；\s]+", s.strip()) if p]
        if not pts or any(len(q) != 2 for q in pts):
            raise ValueError
    except ValueError:
        die(f"--centers 写成 x,y;x,y（画面比例），比如 0.46,0.42;0.3,0.45（你写的是 {s}）")
    return [pts[i % len(pts)] for i in range(n)]


def cmd_montage(a):
    files = list(a.files or [])
    if a.dir:
        if not os.path.isdir(a.dir):
            die(f"找不到文件夹：{a.dir}")
        nat = lambda x: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", x)]
        files += [os.path.join(a.dir, x) for x in sorted(os.listdir(a.dir), key=nat)
                  if os.path.splitext(x)[1].lower() in IMAGE_EXT | VIDEO_EXT]
    if not files:
        die("没有素材：在后面直接列文件，或用 --dir 给文件夹")
    for f in files:
        check_in(f)
    check_out(a.out, *files, a.music)
    files = files * a.loop
    n, fps = len(files), a.fps
    if a.beats:
        if not a.bpm:
            die("--beats 要同时给 --bpm")
        b = [int(round(i * a.beats * 60.0 / a.bpm * fps)) for i in range(n + 1)]   # 按累计时间取整，不会越切越偏
        lens = [b[i + 1] - b[i] for i in range(n)]
    elif a.accel:
        try:
            f0, f1 = (float(x) for x in a.accel.replace("：", ":").split(":"))
        except ValueError:
            die(f"--accel 写成 起始帧数:结束帧数，比如 6:3（你写的是 {a.accel}）")
        lens = [max(1, int(round(f0 + (f1 - f0) * i / max(n - 1, 1)))) for i in range(n)]
    else:
        lens = [a.frames] * n
    W, H = parse_size(a.size)
    start = parse_time(a.from_, a.bpm, fps, "--from")
    music_start = parse_time(a.music_start, a.bpm, fps, "--music-start")
    w = Writer(a.out, W, H, fps, a.music, music_start)
    frozen = {}
    if a.ghost:
        centers = parse_centers(a.centers, n)
        pushes = [a.push] * n
        if a.push_last is not None:
            pushes[-1] = a.push_last
        px = max(1, int(round(8 * W / 1152)))
        out3 = lambda u: 1 - (1 - min(max(u, 0.0), 1.0)) ** 3
        prev = None                                     # 上一张最后一帧、它最后的放大倍数、中心
        for i, (path, L) in enumerate(zip(files, lens)):
            cx, cy = centers[i]
            sc = lambda u, i=i, L=L: 1 + pushes[i] * out3(u / max(L, 1))
            fr, last = Frames(path, start, L, W, H, fps), None
            for j, raw in enumerate(fr):
                base = raw.astype(np.float32)
                subs = [j + d for d in (-0.375, -0.125, 0.125, 0.375) if j + d >= 0] if j < 4 else [j]
                f = np.mean([zoom(base, sc(u), cx, cy) for u in subs], axis=0)   # 头 4 帧带运动模糊
                if j < 3:                                                         # 新一张带一层大一圈的虚影收回
                    f = lighten(f, zoom(base, sc(j) * (1 + 0.12 * (1 - j / 3)), cx, cy), 0.6 * (1 - j / 3))
                    if prev is not None:                                          # 上一张继续放大、变淡，留 3 帧残影
                        pf, ps, (pcx, pcy) = prev
                        f = lighten(f, zoom(pf, ps * (1 + 0.05 * (j + 1)), pcx, pcy), (0.75, 0.45, 0.2)[j])
                if j < 2:                                                         # 切点第一帧红蓝错位，第二帧减半
                    f = rgb_split(f, (px, max(1, px // 2))[j])
                w.write(np.clip(f, 0, 255).astype(np.uint8))
                last = base
            frozen[path] = frozen.get(path, 0) + fr.frozen
            prev = (last, sc(L), centers[i])
    else:
        for path, L in zip(files, lens):
            fr = Frames(path, start, L, W, H, fps)
            for j, f in enumerate(fr):
                w.write(zoom(f, 1 + a.drift * j / max(L - 1, 1)) if a.drift else f)
            frozen[path] = frozen.get(path, 0) + fr.frozen
    w.close()
    print("每张的帧数：" + ",".join(map(str, lens)))
    for path, fz in frozen.items():
        report_frozen(path, fz, fps)


def motion_blur(f, length, horizontal=True):
    L = max(1, int(length))
    return f if L <= 1 else cv2.blur(f, (L, 1) if horizontal else (1, L))


def cmd_whip(a):
    for p in (a.a, a.b):
        check_in(p)
    check_out(a.out, a.a, a.b)
    fps = a.fps
    W, H = parse_size(a.size) if a.size else (image_size(a.a) if is_image(a.a) else (probe(a.a)["w"], probe(a.a)["h"]))
    horiz = a.dir in ("left", "right")
    sign = 1 if a.dir in ("right", "down") else -1      # 镜头往右甩 → 画面内容往左跑
    span = W if horiz else H
    na = int(round(parse_time(a.dur_a, a.bpm, fps, "--dur-a") * fps))
    nb = int(round(parse_time(a.dur_b, a.bpm, fps, "--dur-b") * fps))
    N = a.frames
    w = Writer(a.out, W, H, fps)

    def shift(f, d):
        M = np.float32([[1, 0, d], [0, 1, 0]]) if horiz else np.float32([[1, 0, 0], [0, 1, d]])
        return cv2.warpAffine(f, M, (W, H), borderMode=cv2.BORDER_REFLECT)
    fa = Frames(a.a, parse_time(a.a_from, a.bpm, fps, "--a-from"), na, W, H, fps)
    for k, f in enumerate(fa):                          # A：最后 N 帧加速甩出
        j = k - (na - N)
        if j >= 0:
            u = ease((j + 1) / N, "in")
            f = motion_blur(shift(f, -sign * u * span * 0.35), u * span * 0.25, horiz)
        w.write(f)
    fb = Frames(a.b, parse_time(a.b_from, a.bpm, fps, "--b-from"), nb, W, H, fps)
    for k, f in enumerate(fb):                          # B：开头 N 帧从模糊里甩进来、停稳
        if k < N:
            u = 1 - ease((k + 1) / N, "out")
            f = motion_blur(shift(f, sign * u * span * 0.35), u * span * 0.25, horiz)
        w.write(f)
    w.close()
    report_frozen(a.a, fa.frozen, fps)
    report_frozen(a.b, fb.frozen, fps)


def cmd_stutter(a):
    check_in(a.input)
    check_out(a.out, a.input)
    img = is_image(a.input)
    fps = a.fps
    W, H = parse_size(a.size) if a.size else (image_size(a.input) if img else (probe(a.input)["w"], probe(a.input)["h"]))
    if a.piece < 1 or a.count < 1 or a.step < 0:
        die("--piece、--count 至少是 1，--step 不能是负数")
    span = (a.count - 1) * a.step + a.piece + a.tail
    if span > 120:
        die(f"要用到素材里 {span} 帧，太长了（最多 120 帧），把 --piece / --step / --count / --tail 调小")
    fr = Frames(a.input, parse_time(a.from_, a.bpm, fps, "--from"), span, W, H, fps)
    src = list(fr)
    w = Writer(a.out, W, H, fps)
    for p in range(a.count):                            # 每一片从上一片往后 step 帧开始，重复着往前跳
        z = 1 + a.jump * (p % 2)                        # 单数片微微放大、错开一点，"跳"一下
        cx = 0.5 + (a.jump / 3) * (1 if p % 4 == 1 else -1 if p % 4 == 3 else 0)
        for j in range(a.piece):
            f = src[p * a.step + j]
            w.write(zoom(f, z, cx, 0.5) if z != 1 else f)
    last = (a.count - 1) * a.step + a.piece
    for j in range(a.tail):                             # 最后接着正常往下播 tail 帧，"放开"
        w.write(src[last + j])
    w.close()
    print(f"共 {a.count} 片，每片 {a.piece} 帧，每片往前跳 {a.step} 帧，末尾正常播 {a.tail} 帧")
    report_frozen(a.input, fr.frozen, fps)


def cmd_cutaudio(a):
    if not os.path.exists(a.input):
        die(f"找不到文件：{a.input}")
    check_out(a.out, a.input)
    ext = os.path.splitext(a.out)[1].lower()
    if ext not in (".wav", ".m4a", ".aac"):
        die(f"输出要存成 .wav 或 .m4a（你写的是 {ext or '没有扩展名'}）")
    start = parse_time(a.start, a.bpm, 24, "--start")
    dur = parse_time(a.dur, a.bpm, 24, "--dur")
    lead = parse_time(a.lead, a.bpm, 24, "--lead")
    if dur <= 0:
        die("--dur 要大于 0")
    s0 = start - lead
    if s0 < 0:
        die(f"往前多截 {lead} 秒以后起点变成负数了（{s0:.2f} 秒）")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", a.input, "-vn"]
    cmd += ["-c:a", "pcm_s16le", "-ar", "48000"] if ext == ".wav" else ["-c:a", "aac", "-b:a", "320k"]
    if subprocess.run(cmd + [a.out]).returncode:
        die("ffmpeg 截取失败（上面几行英文是原因）")
    got = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", a.out],
                               capture_output=True, text=True).stdout.strip() or 0)
    if got < dur - 0.05:
        print(f"注意：素材到头了，只截到 {got:.2f} 秒")
    print(f"完成：{a.out}（{got:.2f} 秒）。片段第 0 秒 = 原文件第 {s0:.2f} 秒；第 {start:.2f} 秒那句落在片段第 {lead:.2f} 秒")


# ---------------------------------------------------------------- 命令行
def main():
    P = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = P.add_subparsers(dest="cmd", required=True)

    def add(name, fn, helptext, single=False, video=True):
        p = sub.add_parser(name, help=helptext, description=helptext)
        p.set_defaults(fn=fn)
        if single:
            p.add_argument("input", help="输入视频或图片")
            p.add_argument("--from", dest="from_", default="0", help="从输入的第几秒开始用（默认 0）")
            p.add_argument("--dur", help="做几秒（输入是图片时必填；视频默认做到结尾）")
        p.add_argument("--out", "-o", required=True, help="输出文件")
        p.add_argument("--bpm", type=float, help="每分钟多少拍（时间写成 2拍 时需要）")
        if video:
            p.add_argument("--fps", type=float, default=None if single else 24.0,
                           help="输出帧率（处理单个视频时默认跟原视频一样，其余默认 24）")
            p.add_argument("--size", help="输出大小，如 1920x1080（默认跟素材一样）")
        return p

    p = add("sheet", cmd_sheet, "抽帧检查图：均匀抽 N 帧拼成一张图，或把一段时间里的每一帧都排出来", video=False)
    p.add_argument("input", help="要检查的视频")
    p.add_argument("--count", type=int, default=24, help="均匀抽几帧（默认 24）")
    p.add_argument("--every-frame", action="store_true", help="把 --start 到 --end 之间的每一帧都排出来")
    p.add_argument("--start", default="0", help="--every-frame 时从第几秒开始")
    p.add_argument("--end", help="--every-frame 时到第几秒（默认到结尾，最多 240 帧）")
    p.add_argument("--cols", type=int, default=6, help="每行几格（默认 6）")
    p.add_argument("--thumb", type=int, default=320, help="每格多宽（像素，默认 320）")

    p = add("montage", cmd_montage, "点名蒙太奇 / 闪切：一组图片或视频按固定帧数、越切越快或按拍拼起来")
    p.add_argument("files", nargs="*", help="素材文件（也可以用 --dir）")
    p.add_argument("--dir", help="素材文件夹（按文件名里的数字大小排序：2 排在 10 前面）")
    p.add_argument("--frames", type=int, default=6, help="每张几帧（默认 6）")
    p.add_argument("--accel", help="越切越快：起始帧数:结束帧数，如 6:3")
    p.add_argument("--beats", type=float, help="每张几拍，如 0.5（要配 --bpm）")
    p.add_argument("--from", dest="from_", default="0", help="视频素材从第几秒开始用（默认 0）")
    p.add_argument("--drift", type=float, default=0.0, help="每张内部轻微推近的幅度，如 0.03")
    p.add_argument("--loop", type=int, default=1, help="整组重复几遍")
    p.add_argument("--ghost", action="store_true", help="每张快推 + 上一张残影 + 新一张虚影收回 + 切点红蓝错位（代替 --drift）")
    p.add_argument("--push", type=float, default=0.08, help="--ghost 时每张往里推多少（0.08 = 推 8%%）")
    p.add_argument("--push-last", type=float, help="--ghost 时最后一张推多少（收尾重音，比如 0.14）")
    p.add_argument("--centers", help="--ghost 时每张往哪推（脸的位置，画面比例），如 \"0.46,0.42;0.3,0.45\"；不够就循环用")
    p.add_argument("--music", help="配上音乐")
    p.add_argument("--music-start", default="0", help="音乐从第几秒开始")
    p.set_defaults(size="1920x1080")

    p = add("strobe", cmd_strobe, "频闪：亮 N 帧、暗 M 帧交替", single=True)
    p.add_argument("--on", type=int, default=6, help="亮几帧（默认 6）")
    p.add_argument("--off", type=int, default=3, help="暗几帧（默认 3）")
    p.add_argument("--dark", type=float, default=0.0, help="暗帧亮度 0~1（默认 0 = 全黑）")
    p.add_argument("--start", default="0", help="从第几秒开始闪")
    p.add_argument("--end", help="到第几秒停")

    p = add("punch", cmd_punch, "冲推：在指定时间点猛地推近（带动态模糊）", single=True)
    p.add_argument("--at", required=True, help="时间点，逗号分隔，如 0.25,1.0 或 1拍,3拍")
    p.add_argument("--scale", type=float, default=1.15, help="每次推近多少：1.15 = 放大到 1.15 倍（默认一次比一次近）")
    p.add_argument("--ramp", type=int, default=3, help="推的过程几帧（默认 3）")
    p.add_argument("--reset", action="store_true", help="每次都从原大小重新推")

    p = add("whip", cmd_whip, "甩镜转场：A 结尾甩出、B 开头甩入")
    p.add_argument("--a", required=True, help="甩出的那段（A）")
    p.add_argument("--b", required=True, help="甩入的那段（B）")
    p.add_argument("--a-from", default="0", help="A 从第几秒开始用")
    p.add_argument("--b-from", default="0", help="B 从第几秒开始用")
    p.add_argument("--dur-a", default="1", help="A 段用几秒（甩出包含在内）")
    p.add_argument("--dur-b", default="1", help="B 段用几秒（甩入包含在内）")
    p.add_argument("--frames", type=int, default=5, help="甩出 / 甩入各几帧")
    p.add_argument("--dir", default="right", choices=["left", "right", "up", "down"], help="镜头往哪边甩")

    p = add("glitch", cmd_glitch, "画面故障：指定时间点几帧红蓝错位 + 横条撕裂", single=True)
    p.add_argument("--at", required=True, help="时间点，逗号分隔，如 0.4,1.2")
    p.add_argument("--frames", type=int, default=4, help="每个时间点持续几帧（默认 4）")
    p.add_argument("--amount", type=float, default=0.02, help="红蓝错位幅度（画面宽度比例）")
    p.add_argument("--seed", type=int, default=0, help="换个数字，撕裂条的样子就不同")

    p = add("stutter", cmd_stutter, "口吃剪：同一镜头切成几帧一片的碎片，一片片重复着往前跳")
    p.add_argument("input", help="输入视频（或图片）")
    p.add_argument("--from", dest="from_", default="0", help="从素材第几秒开始取")
    p.add_argument("--piece", type=int, default=5, help="每片几帧（默认 5，常用 3–8）")
    p.add_argument("--step", type=int, default=2, help="每片比上一片往后跳几帧（默认 2；0 = 原地重复同一片）")
    p.add_argument("--count", type=int, default=6, help="几片（默认 6）")
    p.add_argument("--jump", type=float, default=0.03, help="单数片微微放大错开的幅度（默认 0.03；0 = 不跳）")
    p.add_argument("--tail", type=int, default=0, help="最后接着正常往下播几帧（默认 0）")

    p = add("cutaudio", cmd_cutaudio, "截一段音频（对白参考音频、配乐片段）；视频也行，取它的声音", video=False)
    p.add_argument("input", help="音频或视频文件")
    p.add_argument("--start", required=True, help="要的那句在原文件第几秒")
    p.add_argument("--dur", required=True, help="截几秒")
    p.add_argument("--lead", default="0", help="往前多截几秒（让那句落在片段第几秒）")

    a = P.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
