# shorts.py
# ------------------------------------------------------------
# SHIP NEWS 쇼츠 자동 생성 모듈 (v2 - 모션 쇼츠)
#   선택한 뉴스 기사 → 대본 → 음성 → 움직이는 장면(파도·배·자막 애니메이션) → mp4
#
# - 기본 동작은 전부 무료 / API 키 불필요
# - 음성(gTTS)은 인터넷이 필요하며, 실패하면 자막만으로 진행
# - 배경음악은 코드로 직접 합성 (저작권 걱정 없음)
# - ANTHROPIC_API_KEY 가 있으면 LLM 으로 내레이션을 다듬음 (선택 사항)
# 작성: 박혜찬 (2001253)
# ------------------------------------------------------------
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920          # 쇼츠 세로 해상도
FPS = 30
SR = 44100                 # 오디오 샘플레이트

NAVY_TOP = (14, 52, 104)
NAVY_BOTTOM = (4, 16, 36)
ACCENT = (255, 204, 77)
WHITE = (255, 255, 255)
GRAY = (196, 208, 224)
MARGIN = 80

# 운영체제별 한글 폰트 후보 (굵은 글꼴, 보통 글꼴)
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf", 0),             # Windows
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc", 0),  # macOS
    ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
     "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 0),                           # Linux / Colab
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 1),                    # Linux (Noto)
]

# 분야별 강조 키워드 (제목에서 노란색으로 표시)
HIGHLIGHT_WORDS = ["LNG", "암모니아", "메탄올", "수소", "친환경", "수주", "자율운항", "컨테이너선",
                   "잠수함", "구축함", "호위함", "운임", "흑자", "적자", "실적", "탈탄소"]


# ==========================================
# 준비: 폰트 / ffmpeg
# ==========================================
_font_cache = {}


def font(size, bold=True):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for b, r, idx in FONT_CANDIDATES:
        path = b if bold else r
        if os.path.exists(path):
            f = ImageFont.truetype(path, size, index=idx)
            _font_cache[key] = f
            return f
    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. shorts.py 의 FONT_CANDIDATES 에 "
        "PC에 있는 한글 폰트(.ttf) 경로를 추가하세요."
    )


def find_ffmpeg():
    try:
        import imageio_ffmpeg            # pip install imageio-ffmpeg (ffmpeg 자동 포함)
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError("ffmpeg 을 찾지 못했습니다. pip install imageio-ffmpeg 를 실행하세요.")


# ==========================================
# 1. 대본 만들기
# ==========================================
def clean_title(title, source=""):
    """구글 뉴스 제목 끝의 ' - 언론사' 제거"""
    title = re.sub(r"<[^>]+>", "", str(title)).strip()
    if source and title.endswith(" - " + source):
        title = title[: -len(" - " + source)]
    return title.strip()


def short_summary(description, title, limit=110):
    """기사 설명에서 앞 문장 1~2개만 뽑아 짧은 요약으로 사용"""
    text = re.sub(r"<[^>]+>", " ", str(description or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text in title or title in text:   # 제목 반복이면 사용하지 않음
        return ""
    sentences = re.split(r"(?<=[.!?다])\s", text)
    summary = " ".join(sentences[:2])
    return summary[:limit] + ("…" if len(summary) > limit else "")


def llm_rewrite(title, summary):
    """API 키가 있을 때만 LLM 으로 말하듯 짧은 내레이션 작성. 실패하면 None"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "다음 조선해양 뉴스를 쇼츠 영상 내레이션용으로 한국어 2문장, 80자 이내로 써줘. "
        "설명 없이 문장만 출력해.\n"
        f"제목: {title}\n내용: {summary}"
    )
    body = json.dumps({
        "model": "claude-haiku-5-5",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as r:
            return json.load(r)["content"][0]["text"].strip()
    except Exception:
        return None


def ship_type(article):
    text = article.get("title", "") + " " + article.get("category", "")
    if "LNG" in text or "가스" in text:
        return "lng"
    if any(k in text for k in ["잠수함", "구축함", "호위함", "군함", "함정", "방산"]):
        return "navy"
    return "container"


def make_script(articles, headline="오늘의 조선해양 뉴스"):
    """기사 목록 → 장면(scene) 목록"""
    n = len(articles)
    scenes = [{
        "kind": "intro",
        "heading": headline,
        "sub": datetime.now().strftime("%Y.%m.%d"),
        "narration": f"{headline}. 꼭 알아야 할 소식 {n}건, 바로 정리해 드립니다.",
        "ship": "container",
    }]
    for i, a in enumerate(articles, 1):
        title = clean_title(a.get("title", ""), a.get("source", ""))
        summary = short_summary(a.get("description", ""), title)
        narration = llm_rewrite(title, summary) or (
            f"{i}번째 소식. {title}." + (f" {summary}" if summary else ""))
        scenes.append({
            "kind": "news",
            "tag": f"NEWS {i}",
            "category": "" if (a.get("category") or "").split(",")[0].strip() in ("", "기타")
                        else (a.get("category") or "").split(",")[0].strip(),
            "heading": title,
            "sub": " · ".join(x for x in [a.get("source", ""), a.get("date", "")] if x),
            "narration": narration,
            "ship": ship_type(a),
        })
    sources = sorted({a.get("source", "") for a in articles if a.get("source")})
    scenes.append({
        "kind": "outro",
        "heading": "더 많은 조선해양 뉴스는\nSHIP NEWS 에서",
        "sub": ("출처: " + ", ".join(sources)) if sources else "",
        "narration": "자세한 내용은 원문 기사에서 확인하세요.",
        "ship": "container",
    })
    return scenes


def caption_chunks(text, max_chars=14):
    """내레이션을 자막 한 덩어리(약 14자)씩 나눔"""
    words, chunks, cur = text.replace("\n", " ").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
        if cur.endswith((".", "!", "?")):
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    return [c.rstrip(".,") for c in chunks]


# ==========================================
# 2. 오디오: 음성 + 배경음악
# ==========================================
def make_voice(text, path):
    """gTTS(무료, 인터넷 필요)로 한국어 음성 생성. 실패하면 False"""
    try:
        from gtts import gTTS
        gTTS(text=text, lang="ko").save(path)
        return os.path.getsize(path) > 0
    except Exception:
        return False


def decode_audio(ffmpeg, path):
    """mp3 → numpy 배열(mono, float)"""
    p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1",
                        "-ar", str(SR), "-"], capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def make_bgm(seconds):
    """잔잔한 배경음악 합성: 코드 패드 + 부드러운 킥 + 하이햇 (저작권 없음)"""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    bpm = 96
    beat = 60 / bpm
    bar = beat * 4
    # C - Am - F - G 진행 (주파수)
    chords = [[261.6, 329.6, 392.0], [220.0, 261.6, 329.6], [174.6, 220.0, 261.6], [196.0, 246.9, 293.7]]
    out = np.zeros(n, dtype=np.float32)
    for k in range(int(seconds / bar) + 1):
        s, e = int(k * bar * SR), min(n, int((k + 1) * bar * SR))
        if s >= n:
            break
        tt = t[s:e] - k * bar
        env = np.minimum(1, tt / 0.3) * np.minimum(1, (bar - tt) / 0.3)
        for f in chords[k % 4]:
            out[s:e] += 0.05 * env * (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * f * 2 * tt))
        out[s:e] += 0.06 * env * np.sin(2 * np.pi * chords[k % 4][0] / 2 * tt)   # 베이스
    for b in range(int(seconds / beat) + 1):
        s = int(b * beat * SR)
        if s >= n:
            break
        L = min(n - s, int(0.25 * SR))
        tt = np.arange(L) / SR
        out[s:s + L] += 0.25 * np.exp(-tt * 18) * np.sin(2 * np.pi * (55 + 60 * np.exp(-tt * 30)) * tt)  # 킥
        hs = int((b + 0.5) * beat * SR)
        if hs < n:
            HL = min(n - hs, int(0.05 * SR))
            out[hs:hs + HL] += 0.03 * np.exp(-np.arange(HL) / SR * 80) * np.random.uniform(-1, 1, HL)  # 하이햇
    fade = int(1.5 * SR)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    return out


def write_wav(path, mono):
    data = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
    stereo = np.repeat(data[:, None], 2, axis=1)
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


# ==========================================
# 3. 그리기 도우미
# ==========================================
def ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def wrap(draw, text, f, max_width):
    """단어 단위 줄바꿈 (한 단어가 너무 길면 글자 단위)"""
    lines = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split():
            cand = (line + " " + word).strip()
            if draw.textlength(cand, font=f) <= max_width:
                line = cand
                continue
            if line:
                lines.append(line)
            line = ""
            for ch in word:
                if draw.textlength(line + ch, font=f) <= max_width:
                    line += ch
                else:
                    lines.append(line)
                    line = ch
        if line:
            lines.append(line)
    return lines


def text_layer(lines, f, color, line_h, highlight=True, stroke=0):
    """여러 줄 텍스트를 투명 레이어로 만들고, 강조 키워드는 노란색으로"""
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    width = int(max([tmp.textlength(l, font=f) for l in lines] + [1])) + stroke * 2 + 4
    img = Image.new("RGBA", (width, line_h * len(lines) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        x, y = stroke, i * line_h
        # 단어별로 색을 달리하여 그림
        for j, word in enumerate(line.split(" ")):
            token = word + (" " if j < len(line.split(" ")) - 1 else "")
            c = ACCENT if highlight and any(k in word for k in HIGHLIGHT_WORDS) else color
            d.text((x, y), token, font=f, fill=c, stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += d.textlength(token, font=f)
    return img


def paste_anim(frame, layer, x, y, progress, dy=60):
    """progress 0→1 동안 아래에서 위로 올라오며 나타남"""
    if progress <= 0:
        return
    p = ease_out(progress)
    lay = layer
    if p < 1:
        lay = layer.copy()
        alpha = lay.getchannel("A").point(lambda a: int(a * p))
        lay.putalpha(alpha)
    frame.alpha_composite(lay, (int(x), int(y + dy * (1 - p))))


def make_background():
    bg = Image.new("RGB", (W, H))
    arr = np.zeros((H, W, 3), dtype=np.uint8)
    t = np.linspace(0, 1, H)[:, None]
    for k in range(3):
        arr[:, :, k] = (NAVY_TOP[k] * (1 - t) + NAVY_BOTTOM[k] * t).astype(np.uint8)
    bg = Image.fromarray(arr).convert("RGBA")
    d = ImageDraw.Draw(bg)
    # 은은한 별(점) 장식
    rng = np.random.default_rng(7)
    for _ in range(70):
        x, y, r = rng.integers(0, W), rng.integers(0, 1100), rng.integers(1, 4)
        v = int(rng.integers(70, 150))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(v, v + 10, v + 30))
    return bg


def draw_ship(d, x, base_y, kind, bob):
    """간단한 배 실루엣 (컨테이너선 / LNG선 / 군함)"""
    y = base_y + bob
    hull = [(x, y), (x + 520, y), (x + 470, y + 70), (x + 40, y + 70)]
    d.polygon(hull, fill=(20, 32, 52))
    d.rectangle([x + 10, y - 6, x + 515, y + 4], fill=(200, 60, 60))
    if kind == "lng":
        for i in range(4):
            cx = x + 90 + i * 100
            d.pieslice([cx - 45, y - 80, cx + 45, y + 10], 180, 360, fill=(230, 236, 244))
        d.rectangle([x + 440, y - 90, x + 495, y], fill=(230, 236, 244))
    elif kind == "navy":
        d.polygon([(x + 120, y), (x + 160, y - 60), (x + 330, y - 60), (x + 360, y)], fill=(120, 132, 148))
        d.rectangle([x + 220, y - 120, x + 270, y - 60], fill=(120, 132, 148))
        d.line([(x + 245, y - 120), (x + 245, y - 170)], fill=(120, 132, 148), width=6)
        d.rectangle([x + 400, y - 18, x + 470, y - 8], fill=(120, 132, 148))
    else:
        colors = [(232, 93, 76), (66, 133, 244), (251, 188, 5), (52, 168, 83), (171, 71, 188)]
        for col in range(7):
            for row in range(3):
                cx = x + 40 + col * 52
                d.rectangle([cx, y - 34 * (row + 1), cx + 48, y - 34 * row - 4],
                            fill=colors[(col * 2 + row) % len(colors)])
        d.rectangle([x + 420, y - 120, x + 480, y], fill=(235, 238, 242))
        d.rectangle([x + 425, y - 110, x + 475, y - 96], fill=(40, 60, 90))


_ship_cache = {}


def ship_layer(kind, scale=1.45):
    """배 그림을 한 번만 그려 두고 크기를 키워 재사용"""
    if kind not in _ship_cache:
        lay = Image.new("RGBA", (560, 300), (0, 0, 0, 0))
        draw_ship(ImageDraw.Draw(lay), 20, 200, kind, 0)
        _ship_cache[kind] = lay.resize((int(560 * scale), int(300 * scale)), Image.LANCZOS)
    return _ship_cache[kind]


def draw_waves(d, t):
    """여러 겹의 파도가 계속 흘러감"""
    layers = [(1560, 26, 0.9, (22, 70, 130)), (1630, 22, 1.3, (16, 54, 104)), (1710, 18, 1.8, (9, 36, 74))]
    for base, amp, speed, color in layers:
        pts = [(0, H)]
        for x in range(0, W + 40, 40):
            y = base + amp * math.sin(x / 140 + t * speed * 2) + amp * 0.5 * math.sin(x / 61 - t * speed * 3)
            pts.append((x, y))
        pts.append((W, H))
        d.polygon(pts, fill=color)


# ==========================================
# 4. 장면별 레이어 준비 & 프레임 그리기
# ==========================================
def prepare_scene(sc):
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    L = {}
    if sc["kind"] == "intro":
        L["top"] = text_layer(["오늘의 핵심만"], font(52), GRAY, 70, highlight=False)
        L["head"] = text_layer(wrap(tmp, sc["heading"], font(96), W - MARGIN * 2), font(96), WHITE, 124, False)
        L["sub"] = text_layer([sc["sub"]], font(44, False), GRAY, 60, False)
    elif sc["kind"] == "news":
        tag = sc["tag"] + (f"  {sc['category']}" if sc["category"] else "")
        L["tag"] = text_layer([tag], font(44), (6, 22, 46), 60, False)
        L["head"] = text_layer(wrap(tmp, sc["heading"], font(80), W - MARGIN * 2)[:5], font(80), WHITE, 108)
        L["sub"] = text_layer([sc["sub"]], font(40, False), GRAY, 56, False)
    else:
        L["head"] = text_layer(wrap(tmp, sc["heading"], font(84), W - MARGIN * 2), font(84), WHITE, 112, False)
        L["sub"] = text_layer(wrap(tmp, sc["sub"], font(40, False), W - MARGIN * 2)[:3], font(40, False), GRAY, 56, False)
        L["note"] = text_layer(["본 영상은 기사 내용을 요약한 것입니다"], font(34, False), GRAY, 48, False)
    # 화면 가운데를 채우는 큰 배경 글자
    word = {"intro": "NEWS", "outro": "SHIP NEWS"}.get(sc["kind"]) or sc.get("category") or "NEWS"
    if sc["kind"] == "news":   # 인트로는 큰 숫자가 있어 생략
        L["watermark"] = text_layer([word], font(230), (34, 72, 128), 260, False)
    # 자막 덩어리
    caps = caption_chunks(sc["narration"])
    L["caps"] = [text_layer([c], font(72), WHITE, 94, True, stroke=6) for c in caps]
    L["cap_weights"] = [max(1, len(c)) for c in caps]
    return L


def draw_frame(bg, sc, L, t, dur, scene_idx, n_scenes, global_t, n_news):
    frame = bg.copy()
    d = ImageDraw.Draw(frame)

    # 상단 진행바 (장면 수만큼 칸, 현재 칸이 차오름)
    gap, top = 12, 70
    seg = (W - MARGIN * 2 - gap * (n_scenes - 1)) / n_scenes
    for i in range(n_scenes):
        x0 = MARGIN + i * (seg + gap)
        d.rounded_rectangle([x0, top, x0 + seg, top + 10], radius=5, fill=(70, 92, 128))
        fill = 1.0 if i < scene_idx else (min(1.0, t / dur) if i == scene_idx else 0)
        if fill > 0:
            d.rounded_rectangle([x0, top, x0 + seg * fill, top + 10], radius=5, fill=WHITE)

    # 배 (천천히 오른쪽으로 이동, 파도에 따라 흔들림)
    # 배경에 크게 깔리는 분야 글자 (천천히 흐름)
    if L.get("watermark") is not None:
        wm = L["watermark"]
        frame.alpha_composite(wm, (int(MARGIN - 30 - (global_t * 14) % 60), 820))

    # 배: 화면 안에서 출발해 천천히 오른쪽으로 항해, 파도에 따라 위아래로 흔들림
    ship = ship_layer(sc["ship"])
    ship_x = ((830 + global_t * 26) % (W + ship.width)) - ship.width
    frame.alpha_composite(ship, (int(ship_x), int(1585 - ship.height + 8 * math.sin(global_t * 2.2))))
    draw_waves(d, global_t)

    if sc["kind"] == "intro":
        paste_anim(frame, L["top"], MARGIN, 330, t / 0.4)
        # 큰 숫자 (0 → N 카운트업)
        num = str(min(n_news, int(n_news * ease_out(t / 0.9)) + (1 if t > 0.9 else 0)))
        num_layer = text_layer([num], font(300), ACCENT, 320, False)
        paste_anim(frame, num_layer, MARGIN - 10, 400, (t - 0.1) / 0.4, dy=40)
        unit = text_layer(["건 뉴스 한눈에"], font(72), WHITE, 94, False)
        paste_anim(frame, unit, MARGIN + num_layer.width + 10, 590, (t - 0.3) / 0.4)
        paste_anim(frame, L["head"], MARGIN, 800, (t - 0.5) / 0.5)
        paste_anim(frame, L["sub"], MARGIN, 800 + L["head"].height + 30, (t - 0.8) / 0.5)
    elif sc["kind"] == "news":
        # 태그(노란 알약 모양)가 왼쪽에서 들어옴
        p = ease_out(t / 0.35)
        tw = L["tag"].width + 50
        x = MARGIN - (tw + MARGIN) * (1 - p)
        d.rounded_rectangle([x, 200, x + tw, 280], radius=40, fill=ACCENT)
        frame.alpha_composite(L["tag"], (int(x + 25), 210))
        paste_anim(frame, L["head"], MARGIN, 340, (t - 0.15) / 0.45)
        hy = 340 + L["head"].height + 20
        if t > 0.5:
            bar_w = int(160 * ease_out((t - 0.5) / 0.4))
            d.rectangle([MARGIN, hy, MARGIN + bar_w, hy + 10], fill=ACCENT)
        paste_anim(frame, L["sub"], MARGIN, hy + 40, (t - 0.6) / 0.4, dy=30)
    else:
        paste_anim(frame, L["head"], MARGIN, 420, t / 0.5)
        paste_anim(frame, L["sub"], MARGIN, 420 + L["head"].height + 50, (t - 0.4) / 0.5)
        paste_anim(frame, L["note"], MARGIN, 1340, (t - 0.8) / 0.5, dy=20)

    # 내레이션 자막: 글자 수 비율로 시간 배분, 등장할 때 살짝 튀어나옴
    caps, wts = L["caps"], L["cap_weights"]
    if caps and sc["kind"] != "outro":
        start, total = 0.25, max(0.5, dur - 0.45)
        acc, idx = 0.0, len(caps) - 1
        for i, wgt in enumerate(wts):
            span = total * wgt / sum(wts)
            if t < start + acc + span:
                idx = i
                break
            acc += span
        local = max(0.0, t - start - acc)
        cap = caps[idx]
        s = 0.85 + 0.15 * ease_out(local / 0.15)
        cw, ch = int(cap.width * s), int(cap.height * s)
        capr = cap.resize((cw, ch)) if s < 1 else cap
        cy = 1060
        box = Image.new("RGBA", (cw + 60, ch + 20), (0, 0, 0, 130))
        frame.alpha_composite(box, ((W - cw - 60) // 2, cy - 10))
        frame.alpha_composite(capr, ((W - cw) // 2, cy))

    # 하단 로고
    d.text((MARGIN, H - 120), "SHIP NEWS", font=font(40), fill=ACCENT)
    return frame.convert("RGB")


# ==========================================
# 5. 전체 조립
# ==========================================
def build_shorts(articles, out_dir="shorts_output", headline="오늘의 조선해양 뉴스",
                 use_voice=True, use_bgm=True, progress=None):
    """
    articles: [{"title","description","source","date","category"}, ...]
    반환: {"video": mp4 경로, "has_voice": bool, "scenes": 장면 수, "seconds": 길이}
    progress: (0~1 사이 값, 메시지) 를 받는 함수 (선택)
    """
    if not articles:
        raise ValueError("기사를 1건 이상 선택하세요.")
    report = progress or (lambda v, m: None)

    ffmpeg = find_ffmpeg()
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="shorts_")
    scenes = make_script(articles, headline)
    n_news = len(articles)

    # 1) 음성 만들고 장면 길이 결정
    voices, voiced = [], 0
    for i, sc in enumerate(scenes):
        report(0.1 * i / len(scenes), f"음성 만드는 중 ({i + 1}/{len(scenes)})")
        audio = None
        mp3 = os.path.join(work, f"v{i}.mp3")
        if use_voice and make_voice(sc["narration"], mp3):
            audio = decode_audio(ffmpeg, mp3)
            if len(audio) == 0:
                audio = None
        if audio is not None:
            voiced += 1
            sc["dur"] = len(audio) / SR + 0.6
        else:
            sc["dur"] = max(3.2, min(7.0, len(sc["narration"]) / 9))
        if sc["kind"] == "intro":
            sc["dur"] = max(sc["dur"], 3.0)
        voices.append(audio)

    total = sum(sc["dur"] for sc in scenes)

    # 2) 오디오 트랙 (음성 + 배경음악)
    track = np.zeros(int(total * SR) + SR, dtype=np.float32)
    pos = 0.0
    for sc, audio in zip(scenes, voices):
        if audio is not None:
            s = int((pos + 0.2) * SR)
            track[s:s + len(audio)] += audio[: len(track) - s]
        pos += sc["dur"]
    if use_bgm:
        bgm = make_bgm(len(track) / SR)
        track += bgm * (0.35 if voiced else 0.8)
    wav = os.path.join(work, "audio.wav")
    write_wav(wav, track[: int(total * SR)])

    # 3) 프레임을 그려 ffmpeg 으로 바로 전송
    out = os.path.join(out_dir, f"shorts_{datetime.now():%Y%m%d_%H%M%S}.mp4")
    cmd = [ffmpeg, "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    bg = make_background()
    total_frames = int(total * FPS)
    frame_no = 0
    prev_last = last = None
    try:
        for si, sc in enumerate(scenes):
            L = prepare_scene(sc)
            prev_last = last
            n = int(round(sc["dur"] * FPS))
            for k in range(n):
                t = k / FPS
                img = draw_frame(bg, sc, L, t, sc["dur"], si, len(scenes), frame_no / FPS, n_news)
                # 장면 전환: 첫 0.2초는 이전 장면에서 서서히 바뀜
                if prev_last is not None and k < 6:
                    img = Image.blend(prev_last, img, (k + 1) / 7)
                proc.stdin.write(img.tobytes())
                last = img
                frame_no += 1
                if frame_no % 15 == 0:
                    report(0.1 + 0.88 * frame_no / max(1, total_frames),
                           f"영상 만드는 중 ({frame_no}/{total_frames} 프레임)")
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="ignore")
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg 오류:\n" + err[-800:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report(1.0, "완료")
    return {"video": out, "has_voice": voiced == len(scenes),
            "scenes": len(scenes), "seconds": round(total, 1)}


# 단독 테스트: python shorts.py
if __name__ == "__main__":
    sample = [
        {"title": "HD현대, 암모니아 추진 LNG선 2척 수주 - 테스트뉴스",
         "description": "국내 주요 조선소들이 암모니아 추진 친환경 선박 수주를 늘리고 있다. 국제 탄소 규제 강화가 배경으로 꼽힌다.",
         "source": "테스트뉴스", "date": "2026-11-13", "category": "LNG선"},
        {"title": "자율운항 선박 실증 해역 지정 논의 본격화",
         "description": "자율운항 선박 실증을 위한 전용 해역 지정이 논의되고 있다.",
         "source": "샘플일보", "date": "2026-11-12", "category": "기타"},
        {"title": "차세대 잠수함 건조 사업 본격 착수",
         "description": "차세대 잠수함 건조 사업이 본격적으로 시작됐다. 관련 기자재 업계도 분주하다.",
         "source": "샘플경제", "date": "2026-11-11", "category": "방산"},
    ]
    r = build_shorts(sample, progress=lambda v, m: None)
    print(r)
