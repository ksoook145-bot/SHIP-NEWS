"""카드뉴스 이미지 만들기 (1080×1350 PNG) — [카드뉴스 추가]

- 원문을 문장 단위로 끊어 완성된 문장만 모은 초안을 만들고, 화면에서 직접 고칠 수 있게 합니다.
- 제목·요약·키워드·출처·날짜를 넣어 PNG로 그립니다. 폰트와 줄바꿈은 shorts.py 의 함수를 재사용합니다.
"""
import base64
import math
import io
import re

from PIL import Image, ImageDraw

import shorts  # font(), wrap(), HIGHLIGHT_WORDS 재사용

W, H = 1080, 1350
MARGIN = 84
NAVY_TOP = (14, 52, 104)
NAVY_BOTTOM = (4, 16, 36)
ACCENT = (56, 189, 248)
WHITE = (255, 255, 255)
SOFT = (226, 232, 240)
MUTED = (148, 163, 184)
PILL_BG = (30, 64, 118)
PILL_TEXT = (125, 211, 252)
SUMMARY_MAX = 220          # 요약 글자 수 한도
TITLE_MAX = 90             # 제목 글자 수 한도

IMPACT_LABEL = {"High": "고영향", "Mid": "중영향", "Low": "저영향"}
IMPACT_COLOR = {"High": (220, 38, 38), "Mid": (217, 119, 6), "Low": (71, 85, 105)}


def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


BAD_TEXT = re.compile(r"rgb\(|rgba\(|\{|\}|https?://|www\.|#[0-9a-fA-F]{3,6}\b|\bfunction\b|\bvar\b|\bpx\b|;|&nbsp|\\u[0-9a-fA-F]{4}")


def is_clean_sentence(s):
    """웹 코드, 메뉴 나열(· 여러 개), 목록식 글이 아니면 True."""
    if BAD_TEXT.search(s):
        return False
    if s.count("·") >= 2 or s.count("|") >= 2:
        return False
    if s.count(",") >= 6:
        return False
    return True


def auto_summary(description, title="", max_chars=SUMMARY_MAX):
    """원문을 문장 단위로 끊어, 한도 안에 들어가는 완성된 문장만 모은다.
    완성된 문장이 하나도 없으면 빈 문자열을 돌려주고, 화면에서 직접 쓰도록 한다."""
    text = clean_text(description) or clean_text(title)
    sentences = re.findall(r"[^.!?。]+[.!?。]+", text)
    out = ""
    for s in sentences:
        s = s.strip()
        if len(s) < 8 or not is_clean_sentence(s):
            continue
        cand = (out + " " + s).strip()
        if len(cand) > max_chars:
            break
        out = cand
    return out


def ready_summary(description, title="", max_chars=SUMMARY_MAX):
    """자동 요약을 만든다. 완성된 문장이 없으면 기사 제목을 한 문장으로 쓴다."""
    s = auto_summary(description, title, max_chars)
    if s:
        return s
    t = clean_text(title)
    if t and not re.search(r"[.!?。]$", t):
        t += "."
    return t[:max_chars]


UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
SKIP_WORDS = ("무단 전재", "재배포 금지", "저작권", "기자 =", "기자=", "@", "©", "사진=", "사진 =")


def resolve_google_news(url):
    """구글 뉴스 링크(news.google.com/rss/articles/...)에서 원문 주소를 꺼낸다.
    링크 안에 원문 주소가 들어 있는 경우만 풀고, 못 풀면 원래 주소를 돌려준다."""
    if not url or "news.google.com" not in url:
        return url
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return url
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return url
    found = re.search(rb"https?://[\x21-\x7e]+", raw)
    if found:
        return found.group(0).decode("ascii", "ignore")
    return _decode_with_library(url)


def _decode_with_library(url):
    """최신 구글 뉴스 링크는 googlenewsdecoder 패키지로 원문 주소를 푼다.
    패키지가 없거나 실패하면 원래 주소를 돌려준다."""
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        real = res.get("decoded_url", "") if isinstance(res, dict) else ""
        if res.get("status") and real.startswith("http") and "news.google.com" not in real:
            return real
    except Exception:
        pass
    return url


def fetch_article(url, timeout=8):
    """원문 페이지에서 (본문 문단, 페이지 요약 meta 설명)을 돌려준다. 실패하면 ('', '')."""
    if not url:
        return "", ""
    try:
        import html as htmllib
        import requests
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        r.encoding = r.apparent_encoding or r.encoding
        page = r.text
    except Exception:
        return "", ""
    meta = ""
    m = re.search(r'(?is)<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*content=["\'](.*?)["\']', page)
    if m:
        meta = clean_text(htmllib.unescape(m.group(1)))
    page = re.sub(r"(?is)<(script|style|noscript|header|footer|nav|figcaption)[^>]*>.*?</\1>", " ", page)
    texts = []
    for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page):
        t = clean_text(htmllib.unescape(re.sub(r"<[^>]+>", " ", p)))
        if len(t) < 20 or any(w in t for w in SKIP_WORDS) or not is_clean_sentence(t):
            continue
        texts.append(t)
    return " ".join(texts), meta


def article_summary(url, description="", title="", max_chars=SUMMARY_MAX):
    """요약 문장과 그 출처를 돌려준다. (요약, 출처 설명)
    1) 기사 본문  2) 원문 페이지의 요약 설명  3) 검색 결과 설명문
    제목은 요약으로 쓰지 않는다. 내용을 못 가져오면 빈 요약을 돌려주고 직접 입력하게 한다."""
    real = resolve_google_news(url)
    if "news.google.com" in real:
        # 구글 중간 페이지는 본문이 없고 '구글 뉴스 소개' 문구만 있어서 쓰지 않는다.
        body, meta = "", ""
    else:
        body, meta = fetch_article(real)
        if "Google News" in meta:
            meta = ""
    s, src = "", ""
    if body:
        s, src = auto_summary(body, "", max_chars), "기사 본문에서 요약"
    if not s and meta:
        s, src = auto_summary(meta, "", max_chars), "기사 페이지 요약 설명에서 가져옴"
    if not s:
        s = auto_summary(description, "", max_chars)
        src = "검색 설명문에서 요약"
    if not s:
        return "", "기사 내용을 가져오지 못했습니다. 요약을 직접 입력해 주세요."
    if not is_korean(s):
        ko = auto_summary(to_korean(s), "", max_chars)
        if ko:
            s = ko
            src += " · 한국어로 번역"
        else:
            src += " · 번역 실패, 영어 원문입니다"
    return s, src

def is_korean(text):
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    if not letters:
        return True
    hangul = sum(1 for ch in letters if "가" <= ch <= "힣")
    return hangul / len(letters) >= 0.5


def to_korean(text):
    """영어 등 외국어 문장을 한국어로 옮긴다. 실패하면 원문을 그대로 돌려준다."""
    text = clean_text(text)
    if not text or is_korean(text):
        return text
    try:
        import requests
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text},
            timeout=8,
        )
        r.raise_for_status()
        ko = "".join(part[0] for part in r.json()[0] if part and part[0])
        return clean_text(ko) or text
    except Exception:
        return text


CACHE_DAYS = 7


def _cache_path():
    import os
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "card_summary_cache.json")


def _cache_key(url, title):
    import hashlib
    return hashlib.sha1(f"{url}|{clean_text(title)}".encode("utf-8")).hexdigest()


def _cache_get(key):
    import json, time
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            entry = json.load(f).get(key)
    except Exception:
        return None
    if not entry or time.time() - entry.get("ts", 0) > CACHE_DAYS * 86400:
        return None
    return entry["summary"], entry["src"]


def _cache_put(key, summary, src):
    import json, time
    try:
        path = _cache_path()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[key] = {"summary": summary, "src": src, "ts": time.time()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


AI_MODEL = "google/gemma-4-26b-a4b-it:free"
AI_FALLBACK_MODELS = ["google/gemma-4-31b-it:free"]
AI_PROMPT = (
    "아래는 뉴스 기사 본문입니다. 기사 내용만 보고 한국어로 요약하세요.\n"
    "규칙:\n"
    "- 완성된 문장 2~3개, 전체 220자 이내\n"
    "- 모든 문장은 '다.' 같이 끝맺음이 있어야 함\n"
    "- 기사에 없는 내용은 쓰지 않음\n"
    "- 요약 문장만 출력 (제목, 머리말, 따옴표 금지)\n\n"
    "기사 제목: {title}\n\n기사 본문:\n{body}"
)


def ai_card_summary(url, description, title, api_key, model=AI_MODEL):
    """AI가 기사 본문을 읽고 카드용 한국어 요약을 쓴다. (요약, 출처 설명)
    무료 모델이 429(요청 많음) 등으로 막히면 다음 무료 모델로 넘어간다."""
    if not api_key:
        return "", "OpenRouter 키가 없어 AI 요약을 쓸 수 없습니다."
    ck = _cache_key(url, title)
    cached = _cache_get(ck)
    if cached:
        return cached[0], cached[1] + " (저장된 요약)"
    real = resolve_google_news(url)
    if "news.google.com" in real:
        body, meta = "", ""
    else:
        body, meta = fetch_article(real)
        if "Google News" in meta:
            meta = ""
    source = body or meta or clean_text(description)
    if len(source) < 40:
        return "", "기사 내용을 가져오지 못했습니다. 요약을 직접 입력해 주세요."

    import time
    import requests
    models = [model] + [m for m in AI_FALLBACK_MODELS if m != model]
    last = ""
    for m in models:
        for attempt in range(2):
            try:
                r = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": m,
                        "messages": [{"role": "user", "content": AI_PROMPT.format(title=clean_text(title), body=source[:4000])}],
                        "temperature": 0.2,
                        "max_tokens": 400,
                    },
                    timeout=60,
                )
            except Exception as e:
                last = f"{type(e).__name__}"
                break
            if r.status_code == 200:
                try:
                    out = r.json()["choices"][0]["message"]["content"]
                except Exception:
                    last = "응답 형식 오류"
                    break
                out = clean_text(out).strip('"\'“”')
                s = auto_summary(out, "", SUMMARY_MAX)
                if s:
                    src = f"AI({m})가 기사 본문을 읽고 요약"
                    _cache_put(ck, s, src)
                    return s, src
                last = "완성된 문장 없음"
                break
            try:
                detail = clean_text(r.json().get("error", {}).get("message", ""))[:100]
            except Exception:
                detail = ""
            last = f"오류 {r.status_code} {detail}".strip()
            if r.status_code in (429, 502, 503) and attempt == 0:
                time.sleep(4)
                continue
            break
    return "", f"AI 요약 실패 (무료 모델 {len(models)}개 모두 시도): {last}. 잠시 뒤 다시 누르세요."

def check_summary(text):
    """요약 상태 확인 → (글자 수, 경고 문구 또는 None)"""
    t = clean_text(text)
    if not t:
        return 0, "요약이 비어 있습니다. 직접 입력해 주세요."
    if len(t) > SUMMARY_MAX:
        return len(t), f"{SUMMARY_MAX}자를 넘었습니다. 문장을 줄여 주세요."
    if not re.search(r"[.!?。]$", t):
        return len(t), "문장이 끝나지 않았습니다. 마침표로 끝나게 고쳐 주세요."
    return len(t), None


def keywords_for(title, summary, limit=3):
    text = f"{title} {summary}"
    return [w for w in shorts.HIGHLIGHT_WORDS if w in text][:limit]


def _gradient():
    img = Image.new("RGB", (W, H), NAVY_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(NAVY_TOP[i] + (NAVY_BOTTOM[i] - NAVY_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)
    return img


def _pill(d, x, y, text, font, bg, fg, pad_x=24, pad_y=10):
    """둥근 배지를 그리고 오른쪽 끝 x좌표를 돌려준다."""
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    w, h = r - l, b - t
    d.rounded_rectangle([x, y, x + w + pad_x * 2, y + h + pad_y * 2],
                        radius=(h + pad_y * 2) // 2, fill=bg)
    d.text((x + pad_x - l, y + pad_y - t), text, font=font, fill=fg)
    return x + w + pad_x * 2


def icon_kind(category):
    """카테고리 이름으로 그림 종류를 고른다."""
    cat = str(category or "")
    if any(k in cat for k in ("수주", "LNG", "컨테이너", "해운", "선박", "조선")):
        return "ship"
    if any(k in cat for k in ("실적", "시장", "매출", "주가")):
        return "chart"
    if any(k in cat for k in ("기술", "개발", "엔진")):
        return "gear"
    if any(k in cat for k in ("방산", "정책", "규제", "국방")):
        return "shield"
    if any(k in cat for k in ("친환경", "환경", "탄소", "수소")):
        return "globe"
    return "news"


def draw_emblem(d, cx, cy, r, kind):
    """작은 원형 그림 한 장. 배경 원 + 카테고리 그림."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PILL_BG, outline=ACCENT, width=5)
    s = r * 2  # 그림 기준 크기
    fg = PILL_TEXT

    def P(x, y):
        return (cx + x * s, cy + y * s)

    if kind == "ship":
        d.polygon([P(-0.36, 0.02), P(0.36, 0.02), P(0.27, 0.18), P(-0.27, 0.18)], fill=fg)
        d.rectangle([*P(-0.2, -0.14), *P(0.08, 0.02)], fill=WHITE)
        d.rectangle([*P(0.0, -0.28), *P(0.1, -0.14)], fill=WHITE)
    elif kind == "chart":
        for x, h in ((-0.3, 0.25), (-0.08, 0.45), (0.14, 0.65)):
            d.rectangle([*P(x, 0.25 - h), *P(x + 0.18, 0.25)], fill=fg)
        d.line([P(-0.32, 0.0), P(-0.05, -0.12), P(0.12, -0.02), P(0.34, -0.3)], fill=WHITE, width=max(3, int(r * 0.06)))
    elif kind == "gear":
        d.ellipse([*P(-0.22, -0.22), *P(0.22, 0.22)], outline=fg, width=max(6, int(r * 0.12)))
        for k in range(8):
            a = k * math.pi / 4
            tx, ty = math.cos(a) * 0.3, math.sin(a) * 0.3
            d.ellipse([*P(tx - 0.07, ty - 0.07), *P(tx + 0.07, ty + 0.07)], fill=fg)
        d.ellipse([*P(-0.08, -0.08), *P(0.08, 0.08)], fill=WHITE)
    elif kind == "shield":
        d.polygon([P(-0.3, -0.36), P(0.3, -0.36), P(0.3, 0.02), P(0.0, 0.4), P(-0.3, 0.02)], fill=fg)
        d.line([P(0.0, -0.2), P(0.0, 0.25)], fill=WHITE, width=max(4, int(r * 0.08)))
        d.line([P(-0.14, -0.05), P(0.14, -0.05)], fill=WHITE, width=max(4, int(r * 0.08)))
    elif kind == "globe":
        d.ellipse([*P(-0.36, -0.36), *P(0.36, 0.36)], outline=fg, width=max(5, int(r * 0.08)))
        d.ellipse([*P(-0.16, -0.36), *P(0.16, 0.36)], outline=fg, width=max(4, int(r * 0.06)))
        d.line([P(-0.36, 0.0), P(0.36, 0.0)], fill=fg, width=max(4, int(r * 0.06)))
    else:  # news: 신문
        d.rounded_rectangle([*P(-0.32, -0.36), *P(0.32, 0.36)], radius=int(r * 0.05), outline=fg, width=max(5, int(r * 0.07)))
        for y in (-0.2, -0.05, 0.1, 0.25):
            d.line([P(-0.2, y), P(0.2, y)], fill=fg, width=max(4, int(r * 0.05)))


PHOTO_QUERIES = [
    (("LNG",), "LNG tanker ship"),
    (("컨테이너",), "container ship port"),
    (("방산", "국방", "해군", "잠수함", "함정"), "navy warship"),
    (("엔진",), "ship engine"),
    (("친환경", "탄소", "수소", "풍력"), "green energy wind"),
    (("실적", "시장", "매출", "주가", "수주액"), "stock market chart"),
    (("수주", "조선", "선박", "해운", "조선소", "건조"), "shipbuilding shipyard"),
    (("기술", "개발"), "engineering technology"),
]


def photo_query(title, category=""):
    """기사 제목과 분류에서 사진 검색어(영어)를 고른다."""
    text = f"{title} {category}"
    for words, query in PHOTO_QUERIES:
        if any(w in text for w in words):
            return query
    return "shipyard ship"


def fetch_photo(title, category, api_key):
    """Pexels에서 기사와 맞는 사진 한 장을 받아 바이트로 돌려준다. 실패하면 None.
    같은 검색어는 data/photos 폴더에 저장해 두고 다시 쓴다."""
    if not api_key:
        return None
    import os
    import hashlib
    import requests
    query = photo_query(title, category)
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "photos")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, hashlib.sha1(query.encode("utf-8")).hexdigest() + ".jpg")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 5, "orientation": "square"},
            timeout=12,
        )
        if r.status_code != 200:
            return None
        photos = r.json().get("photos", [])
        if not photos:
            return None
        img = requests.get(photos[0]["src"]["medium"], timeout=12).content
        with open(path, "wb") as f:
            f.write(img)
        return img
    except Exception:
        return None


def _photo_circle(img, cx, cy, r, photo_bytes):
    """사진을 정사각형으로 자르고 동그랗게 붙인다."""
    import io
    src = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = src.size
    side = min(w, h)
    src = src.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    src = src.resize((r * 2, r * 2))
    mask = Image.new("L", (r * 2, r * 2), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, r * 2 - 1, r * 2 - 1], fill=255)
    img.paste(src, (cx - r, cy - r), mask)
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=5)


def _cover_photo(photo_bytes):
    """사진을 카드 크기(1080×1350)로 가운데를 잘라 꽉 채운다."""
    import io
    src = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = src.size
    scale = max(W / w, H / h)
    nw, nh = int(w * scale) + 1, int(h * scale) + 1
    src = src.resize((nw, nh))
    left, top = (nw - W) // 2, (nh - H) // 2
    return src.crop((left, top, left + W, top + H))


def _dark_overlay(top_alpha=110, bottom_alpha=225, start_y=380):
    """사진 위에 아래쪽이 더 어두운 막을 만든다. 글자가 잘 보이게 하는 용도."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(H):
        t = min(1.0, max(0.0, (y - start_y) / (H - start_y)))
        a = int(top_alpha + (bottom_alpha - top_alpha) * t)
        d.line([(0, y), (W, y)], fill=(4, 14, 34, a))
    return ov


def render_card(title, summary, category="뉴스", impact="Mid", keywords=None, source="", date="", photo=None):
    """카드 한 장을 PNG 바이트로 돌려준다.
    사진이 있으면 사진을 배경으로 깔고 그 위에 제목·요약을 올린다. 없으면 파란 배경."""
    title = clean_text(title)[:TITLE_MAX]
    summary = clean_text(summary)[:SUMMARY_MAX]
    keywords = keywords if keywords is not None else keywords_for(title, summary)

    bg = None
    if photo:
        try:
            bg = _cover_photo(photo)
        except Exception:
            bg = None

    if bg is not None:
        img = Image.alpha_composite(bg.convert("RGBA"), _dark_overlay()).convert("RGB")
    else:
        img = _gradient()
    d = ImageDraw.Draw(img)
    f_brand = shorts.font(34)
    f_pill = shorts.font(28)
    f_title = shorts.font(64)
    f_sum = shorts.font(36, bold=False)
    f_kw = shorts.font(26, bold=False)
    f_meta = shorts.font(26, bold=False)
    text_w = W - MARGIN * 2

    # 상단: 브랜드 + 영향도 배지
    d.text((MARGIN, MARGIN), "SHIP NEWS", font=f_brand, fill=WHITE)

    # 분류 배지
    _pill(d, MARGIN, 200, f"#{category}", f_pill, PILL_BG, PILL_TEXT)

    # 제목: 사진 모드에서는 아래쪽에 놓는다
    y = 700 if bg is not None else 300
    title_lines = shorts.wrap(d, title, f_title, text_w)[:4]
    for line in title_lines:
        d.text((MARGIN, y), line, font=f_title, fill=WHITE)
        y += 84

    # 구분선
    y += 36
    d.rounded_rectangle([MARGIN, y, MARGIN + 140, y + 8], radius=4, fill=ACCENT)

    # 요약 (완성된 문장)
    y += 48
    for line in shorts.wrap(d, summary, f_sum, text_w):
        d.text((MARGIN, y), line, font=f_sum, fill=SOFT)
        y += 56

    if bg is None:
        # 사진이 없을 때만 가운데 그림과 키워드를 넣는다
        if photo is None:
            draw_emblem(d, W // 2, 880, 120, icon_kind(category))
        footer_y = H - MARGIN - 70
        kw_y = footer_y - 90
        kx = MARGIN
        for kw in keywords:
            kx = _pill(d, kx, kw_y, f"#{kw}", f_kw, (15, 23, 42), SOFT, pad_x=20, pad_y=8) + 14
    else:
        footer_y = H - MARGIN - 70


    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
