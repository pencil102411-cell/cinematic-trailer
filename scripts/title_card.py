"""渲染预告片字卡/片名动效，不开 AE 也能出预览或简单成片。

用法:
  python title_card.py --text "剑来" --sub "T H E   O N E" --preset slam --dur 3 --out 片名.mp4
  python title_card.py --text "正|邪" --preset split --colors "#3fb6ff,#ff8a2a" --dur 0.5 --out 正邪.mp4
  python title_card.py --text "JUSTICE" --sub "正义" --preset slam --texture 某帧.png --font C:/Windows/Fonts/impact.ttf --out j.mp4
  python title_card.py --text "敬请期待" --preset fade --bg 尾钩镜头.mp4 --out 叠字.mp4
  python title_card.py --text "铁证" --preset sweep --alpha --out 铁证.mov      # ProRes 4444 透明底，叠到剪辑里

预设:
  fade   推近淡入：6~8 帧淡入，整段缓慢放大，结尾淡出
  slam   砸入：4 帧从 135% 砸到 100% + 模糊 + 白闪 + 衰减抖动 + 发光
  glitch 故障：RGB 错位 + 横向撕裂，开头一阵、中间零星几下
  sweep  光扫：淡入后一道高光斜扫过文字
  split  双色：文字用 | 分段，每段一种颜色和同色发光，入场同 slam

常用参数:
  --text      主文字；\\n 换行；split 预设用 | 分段
  --sub       副标题（英文片名、日期等），小号、拉开字距
  --dur --fps --size 1920x1080
  --font --sub-font --font-size（默认画高 18%）--sub-size（默认主字 22%）--tracking（字距，默认字号 8%）
  --color "#ffffff"  --colors "色1,色2"（split）  --glow 0~1（默认 0.35）
  --texture 图片    用图片填充文字（章节卡"字里有画面"）
  --bg 图片或视频   背景（默认纯黑），--bg-dim 0~1 压暗背景（默认 0.45）
  --alpha           透明底 ProRes 4444 .mov
  --y 0~1           文字中心的垂直位置（默认 0.5）
依赖: ffmpeg；pip install numpy opencv-python pillow
"""
import argparse, os, subprocess, sys
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")
DEFAULT_FONTS = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/NotoSansSC-VF.ttf",
                 "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]


def find_font(path):
    if path:
        return path
    for p in DEFAULT_FONTS:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到可用字体，请用 --font 指定")


def hex_rgb(s):
    s = s.strip().lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def draw_tracked(draw, xy, text, font, tracking, fill):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking


def tracked_width(text, font, tracking):
    return sum(font.getlength(ch) for ch in text) + tracking * max(0, len(text) - 1)


def build_layer(W, H, text, sub, font_path, font_size, sub_size, tracking, colors, y_center, texture, sub_font_path=None):
    """返回 (rgb float32 HxWx3, alpha float32 HxW)，文字居中。"""
    font = ImageFont.truetype(font_path, font_size)
    sfont = ImageFont.truetype(sub_font_path or font_path, sub_size) if sub else None
    lines = text.split("\\n")
    segs_per_line = [ln.split("|") for ln in lines]
    gap = font_size * 0.35
    asc, desc = font.getmetrics()
    line_h = asc + desc
    block_h = line_h * len(lines) + (sub_size * 1.6 if sub else 0)
    y = H * y_center - block_h / 2
    masks = [Image.new("L", (W, H), 0) for _ in range(max(len(s) for s in segs_per_line))]
    for segs in segs_per_line:
        widths = [tracked_width(s, font, tracking) for s in segs]
        total = sum(widths) + gap * (len(segs) - 1)
        x = (W - total) / 2
        for k, s in enumerate(segs):
            draw_tracked(ImageDraw.Draw(masks[k]), (x, y), s, font, tracking, 255)
            x += widths[k] + gap
        y += line_h
    sub_mask = Image.new("L", (W, H), 0)
    if sub:
        st = sub_size * 0.45
        sw = tracked_width(sub, sfont, st)
        draw_tracked(ImageDraw.Draw(sub_mask), ((W - sw) / 2, y + sub_size * 0.3), sub, sfont, st, 255)
    ms = [np.asarray(m, np.float32) / 255 for m in masks]
    sm = np.asarray(sub_mask, np.float32) / 255
    rgb = np.zeros((H, W, 3), np.float32)
    if texture is not None:
        rgb[:] = texture
    else:
        for k, m in enumerate(ms):
            rgb += m[..., None] * np.array(colors[min(k, len(colors) - 1)], np.float32) / 255
    rgb += sm[..., None] * (np.array(colors[0], np.float32) / 255 if texture is None else 0.9)
    alpha = np.clip(sum(ms) + sm, 0, 1)
    # 纹理模式下 rgb 已铺满整幅，这里只保留文字处的颜色
    return np.clip(rgb, 0, 1), alpha


def load_cover(path, W, H):
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不了图片: {path}")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    s = max(W / img.shape[1], H / img.shape[0])
    img = cv2.resize(img, (int(img.shape[1] * s + 1), int(img.shape[0] * s + 1)), interpolation=cv2.INTER_AREA)
    y0, x0 = (img.shape[0] - H) // 2, (img.shape[1] - W) // 2
    return img[y0:y0 + H, x0:x0 + W].astype(np.float32) / 255


def video_frames(path, W, H, n):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf",
                          f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", "-pix_fmt", "rgb24",
                          "-frames:v", str(n), "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    raw = p.stdout.read(); p.wait()
    k = len(raw) // (W * H * 3)
    if k == 0:
        raise SystemExit(f"读不了视频: {path}")
    frames = np.frombuffer(raw[:k * W * H * 3], np.uint8).reshape(k, H, W, 3)
    return [frames[min(i, k - 1)] for i in range(n)]


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def smooth(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def warp(img, scale, dx, dy, W, H):
    M = np.array([[scale, 0, (1 - scale) * W / 2 + dx], [0, scale, (1 - scale) * H / 2 + dy]], np.float32)
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def render(text, out, preset="fade", dur=2.0, fps=24, size=(1920, 1080), sub=None, font=None, font_size=None,
           sub_size=None, tracking=None, color="#ffffff", colors=None, glow=0.35, texture=None, bg=None, bg_dim=0.45,
           alpha_out=False, y_center=0.5, seed=7, sub_font=None):
    W, H = size
    n = max(1, int(round(dur * fps)))
    fp = find_font(font)
    fs = font_size or int(H * 0.18)
    ss = sub_size or max(12, int(fs * 0.22))
    tr = fs * 0.08 if tracking is None else tracking
    if preset == "sweep" and color.lower() in ("#ffffff", "#fff", "white"):
        color = "#a8a8a8"                     # 光扫要在灰/金属色上才看得见
    cols = [hex_rgb(c) for c in (colors.split(",") if colors else [color])]
    # 副标题含中文而主字体是西文字体时，副标题自动换回中文字体
    sfp = sub_font or (find_font(None) if font and sub and any(ord(ch) > 127 for ch in sub) else fp)
    if preset == "split" and not colors:
        cols = [hex_rgb("#3fb6ff"), hex_rgb("#ff8a2a")]
    tex = load_cover(texture, W, H) if texture else None
    rgb, a = build_layer(W, H, text, sub, fp, fs, ss, tr, cols, y_center, tex, sfp)
    layer = np.dstack([rgb * a[..., None], a]).astype(np.float32)   # 预乘 RGBA
    glow_col = np.dstack([rgb, np.ones_like(a)]) if preset == "split" else None
    bg_frames = None
    if bg and not alpha_out:
        ext = os.path.splitext(bg)[1].lower()
        if ext in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            bg_frames = [f.astype(np.float32) / 255 for f in video_frames(bg, W, H, n)]
        else:
            still = load_cover(bg, W, H)
    rng = np.random.default_rng(seed)
    glitch_frames = set(range(0, 6))
    if preset == "glitch":
        for _ in range(max(1, int(dur / 0.7))):
            s0 = int(rng.integers(8, max(9, n - 6)))
            glitch_frames |= {s0, s0 + 1}
    if alpha_out:
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
               "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", out]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", "-preset", "medium", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    out_frames = int(round(0.25 * fps))
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    diag = (xx + yy * 0.6) / (W + H * 0.6)
    del yy, xx
    for i in range(n):
        t = i / fps
        tail = smooth((n - 1 - i) / max(1, out_frames)) if n > out_frames * 3 else 1.0
        scale, dx, dy, op, blur, flash = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
        if preset in ("slam", "split"):
            k = i / max(1, round(fps / 6))           # 约 4 帧@24fps 完成砸入
            scale = 1.35 - 0.35 * ease_out(k) + 0.03 * max(0, t - 4 / 24) / max(dur, 1e-6)
            blur = 12 * (1 - ease_out(k))
            op = smooth(i / 2)
            fi = i - round(fps / 6)
            flash = {0: 0.6, 1: 0.25}.get(fi, 0.0)        # 砸中那一帧白闪，下一帧余光
            amp = 14 * max(0, 1 - i / (fps * 0.5))
            dx, dy = amp * np.sin(i * 2.7), amp * 0.45 * np.cos(i * 3.3)
        elif preset in ("fade", "sweep", "glitch"):
            fin = round(fps / 3.5)
            op = smooth(i / max(1, fin))
            scale = 1.0 + 0.06 * (t / max(dur, 1e-6))
            blur = 6 * (1 - smooth(i / max(1, fin)))
        op *= tail
        L = warp(layer, scale, dx, dy, W, H)
        if blur > 0.3:
            L = cv2.GaussianBlur(L, (0, 0), blur)
        if preset == "glitch" and i in glitch_frames:
            sh = int(rng.integers(6, 22))
            L[..., 0] = np.roll(L[..., 0], sh, axis=1)
            L[..., 2] = np.roll(L[..., 2], -sh, axis=1)
            for _ in range(int(rng.integers(3, 7))):
                y0 = int(rng.integers(0, H - 20)); h = int(rng.integers(6, 40))
                L[y0:y0 + h] = np.roll(L[y0:y0 + h], int(rng.integers(-60, 60)), axis=1)
        if preset == "sweep":
            pos = (t - 0.3) / 1.2                     # 0.3s 后 1.2s 扫过
            if -0.2 < pos < 1.2:
                band = np.exp(-((diag - pos) / 0.035) ** 2)
                L[..., :3] += band[..., None] * L[..., 3:4] * 0.9
        # 发光
        if glow > 0:
            src = warp(glow_col * layer[..., 3:4], scale, dx, dy, W, H) if glow_col is not None else L
            small = cv2.resize(src, (W // 4, H // 4), interpolation=cv2.INTER_AREA)   # 缩小 4 倍做模糊，快 10 倍以上
            g = cv2.GaussianBlur(small, (0, 0), max(1, fs * 0.03)) * glow
            g += cv2.GaussianBlur(small, (0, 0), max(2.5, fs * 0.09)) * glow * 0.6
            G = cv2.resize(g, (W, H), interpolation=cv2.INTER_LINEAR)
        else:
            G = np.zeros_like(L)
        L = L * op
        G = G * op
        if alpha_out:
            col = np.clip(L[..., :3] + G[..., :3] + flash * L[..., 3:4], 0, 1)
            al = np.clip(L[..., 3] + G[..., 3] * 0.6, 0, 1)
            straight = np.where(al[..., None] > 1e-4, col / np.maximum(al[..., None], 1e-4), 0)
            frame = np.dstack([np.clip(straight, 0, 1), al])
            p.stdin.write((frame * 255).astype(np.uint8).tobytes())
            continue
        if bg_frames is not None:
            base = bg_frames[i] * (1 - bg_dim)
        elif bg:
            base = warp(still, 1.0 + 0.05 * t / max(dur, 1e-6), 0, 0, W, H) * (1 - bg_dim)
        else:
            base = np.zeros((H, W, 3), np.float32)
        comp = base * (1 - L[..., 3:4]) + L[..., :3]
        comp = 1 - (1 - comp) * (1 - np.clip(G[..., :3], 0, 1))      # 滤色叠发光
        comp = comp + (1 - comp) * flash                                # 白闪
        p.stdin.write((np.clip(comp, 0, 1) * 255).astype(np.uint8).tobytes())
    p.stdin.close()
    p.wait()
    if p.returncode != 0:
        raise SystemExit("ffmpeg 编码失败")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preset", default="fade", choices=["fade", "slam", "glitch", "sweep", "split"])
    ap.add_argument("--dur", type=float, default=2.0)
    ap.add_argument("--fps", type=float, default=24)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--sub")
    ap.add_argument("--font")
    ap.add_argument("--sub-font")
    ap.add_argument("--font-size", type=int)
    ap.add_argument("--sub-size", type=int)
    ap.add_argument("--tracking", type=float)
    ap.add_argument("--color", default="#ffffff")
    ap.add_argument("--colors")
    ap.add_argument("--glow", type=float, default=0.35)
    ap.add_argument("--texture")
    ap.add_argument("--bg")
    ap.add_argument("--bg-dim", type=float, default=0.45)
    ap.add_argument("--alpha", action="store_true")
    ap.add_argument("--y", type=float, default=0.5)
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.lower().split("x"))
    if a.alpha and not a.out.lower().endswith(".mov"):
        a.out = os.path.splitext(a.out)[0] + ".mov"
    render(a.text, a.out, a.preset, a.dur, a.fps, (W, H), a.sub, a.font, a.font_size, a.sub_size, a.tracking, a.color,
           a.colors, a.glow, a.texture, a.bg, a.bg_dim, a.alpha, a.y, sub_font=a.sub_font)
    print("输出 →", a.out)


if __name__ == "__main__":
    main()
