# app.py (보안 강화 버전 + 쇼츠 생성 기능 추가)
# ------------------------------------------------------------
# [쇼츠 추가] 표시가 붙은 부분이 새로 추가·수정된 곳입니다.
#   1) GNews API 키가 없거나 오류가 나면 구글 뉴스 RSS(무료, 키 불필요)로 자동 검색
#   2) '🎬 쇼츠 생성' 메뉴: 검색 결과·관심기사로 세로형 쇼츠 영상(mp4) 자동 제작
#      (영상 제작 코드는 shorts.py 에 분리)
# ------------------------------------------------------------
import hashlib
import hmac
import os
import re
import sqlite3
import time
import json
import xml.etree.ElementTree as ET   # [쇼츠 추가] RSS 읽기
from email.utils import parsedate_to_datetime  # [쇼츠 추가] RSS 날짜 변환
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote  # [쇼츠 추가] quote
from datetime import datetime
from urllib.parse import urlparse

import streamlit as st

# ==========================================
# 기본 설정
# ==========================================
st.set_page_config(
    page_title="SHIP NEWS",
    page_icon="⚓",
    layout="wide"
)

DB_NAME = "shipnews.db"

# 입력 제한
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_가-힣]{3,20}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 64
MAX_POST_TITLE = 100
MAX_POST_CONTENT = 2000

# 로그인 시도 제한
MAX_FAILS = 5
LOCK_SECONDS = 300  # 5분


# ==========================================
# 데이터베이스
# ==========================================
def get_db():
    return sqlite3.connect(DB_NAME, check_same_thread=False)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # 회원
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user'
        )
    """)

    # 관심 기사
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT,
            source TEXT,
            link TEXT,
            category TEXT,
            created_at TEXT
        )
    """)

    # 게시판
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT,
            content TEXT,
            created_at TEXT
        )
    """)

    # 로그인 실패 기록
    cur.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            username TEXT PRIMARY KEY,
            fails INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ==========================================
# 안전한 출력 / 링크 처리
# ==========================================
def esc(text):
    """마크다운 특수문자를 이스케이프해서 서식·링크·이미지가 해석되지 않게 한다."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~$:<&])", r"\\\1", str(text))


def is_safe_url(url):
    """http / https 링크만 허용 (javascript: 등 차단)."""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def strip_html(text):
    """뉴스 API 결과에 섞인 <b> 같은 태그 제거."""
    return re.sub(r"<[^>]+>", "", str(text))


# ==========================================
# 비밀번호 (salt + scrypt)
# ==========================================
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    """(일치 여부, 해시 업그레이드 필요 여부) 반환"""

    if stored.startswith("scrypt$"):
        try:
            _, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt_hex),
                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
            )
        except ValueError:
            return False, False
        return hmac.compare_digest(dk.hex(), dk_hex), False

    # 기존 SHA-256 해시(이전 버전 가입자): 로그인 성공 시 scrypt로 교체
    legacy = hashlib.sha256(password.encode()).hexdigest()
    ok = hmac.compare_digest(legacy, stored)
    return ok, ok


# 존재하지 않는 아이디도 같은 시간이 걸리도록 하는 더미 해시
DUMMY_HASH = hash_password("dummy-password")


# ==========================================
# 입력 검증
# ==========================================
def validate_username(username):
    if not USERNAME_PATTERN.match(username):
        return "아이디는 3~20자의 한글, 영문, 숫자, 밑줄(_)만 사용할 수 있습니다."
    return None


def validate_password(password):
    if len(password) < MIN_PASSWORD_LEN:
        return f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
    if len(password) > MAX_PASSWORD_LEN:
        return f"비밀번호는 {MAX_PASSWORD_LEN}자 이하여야 합니다."
    return None


# ==========================================
# 회원가입 / 로그인
# ==========================================
def register(username, password):
    conn = get_db()

    try:
        conn.execute(
            "INSERT INTO users(username, password) VALUES (?, ?)",
            (username, hash_password(password))
        )
        conn.commit()
        return True

    except sqlite3.IntegrityError:
        return False

    finally:
        conn.close()


def login(username, password):
    """(user, 오류메시지) 반환. 성공하면 user=(username, role)"""

    username = username.strip()
    conn = get_db()

    try:
        now = time.time()

        attempt = conn.execute(
            "SELECT fails, locked_until FROM login_attempts WHERE username=?",
            (username,)
        ).fetchone()

        if attempt and attempt[1] > now:
            minutes = int(attempt[1] - now) // 60 + 1
            return None, (
                f"로그인 시도가 너무 많습니다. 약 {minutes}분 후 다시 시도하세요."
            )

        row = conn.execute(
            "SELECT username, password, role FROM users WHERE username=?",
            (username,)
        ).fetchone()

        if row:
            ok, needs_upgrade = verify_password(password, row[1])
        else:
            verify_password(password, DUMMY_HASH)
            ok, needs_upgrade = False, False

        if ok:
            if needs_upgrade:
                conn.execute(
                    "UPDATE users SET password=? WHERE username=?",
                    (hash_password(password), row[0])
                )
            conn.execute(
                "DELETE FROM login_attempts WHERE username=?",
                (username,)
            )
            conn.commit()
            return (row[0], row[2]), None

        # 실패 기록
        fails = (attempt[0] if attempt else 0) + 1
        locked_until = 0

        if fails >= MAX_FAILS:
            locked_until = now + LOCK_SECONDS
            fails = 0

        conn.execute(
            "INSERT OR REPLACE INTO login_attempts"
            "(username, fails, locked_until) VALUES (?, ?, ?)",
            (username, fails, locked_until)
        )
        conn.commit()

        return None, "아이디 또는 비밀번호가 잘못되었습니다."

    finally:
        conn.close()


# ==========================================
# 뉴스 분야 자동 분류
# ==========================================
CATEGORY_KEYWORDS = {

    "수주": [
        "수주", "계약", "발주", "건조계약"
    ],

    "LNG선": [
        "LNG", "LNG선", "LNG운반선"
    ],

    "컨테이너선": [
        "컨테이너선", "컨테이너"
    ],

    "방산": [
        "잠수함", "구축함", "호위함",
        "군함", "함정"
    ],

    "친환경": [
        "암모니아", "메탄올", "수소",
        "친환경", "탈탄소"
    ],

    "해운": [
        "해운", "운임", "선사",
        "물동량"
    ],

    "기업·실적": [
        "매출", "영업이익", "실적",
        "흑자", "적자"
    ]
}


def classify_news(title, description=""):

    text = (title + " " + description).lower()

    result = []

    for category, keywords in CATEGORY_KEYWORDS.items():

        for keyword in keywords:

            if keyword.lower() in text:
                result.append(category)
                break

    if not result:
        result.append("기타")

    return result


# ==========================================
# 언론사 출처 분류
# ==========================================
SOURCE_CATEGORIES = {

    "종합통신사": [
        "연합뉴스",
        "뉴시스",
        "뉴스1"
    ],

    "경제매체": [
        "한국경제",
        "매일경제",
        "서울경제",
        "머니투데이"
    ],

    "종합일간지": [
        "조선일보",
        "중앙일보",
        "동아일보",
        "한겨레",
        "경향신문"
    ],

    "산업·전문매체": [
        "조선비즈",
        "더구루"
    ]
}


def classify_source(source):

    for category, companies in SOURCE_CATEGORIES.items():

        for company in companies:

            if company in source:
                return category

    return "기타 언론매체"


# ==========================================
# 뉴스 API - GNews
# ==========================================
def get_gnews_key():
    """[쇼츠 추가] secrets.toml 파일이 없어도 오류 없이 빈 값 반환"""
    try:
        return st.secrets.get("GNEWS_API_KEY", "")
    except Exception:
        return ""


def search_news_rss(query):
    """[쇼츠 추가] 구글 뉴스 RSS 검색 (무료, API 키 불필요)"""

    url = (
        "https://news.google.com/rss/search?q=" + quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )

    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (SHIP NEWS)"})
        with urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read())
    except Exception:
        st.error("구글 뉴스 RSS 에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
        return []

    news_list = []

    for item in root.iter("item"):
        source_el = item.find("source")
        source = source_el.text if source_el is not None and source_el.text else "출처 미상"
        title = item.findtext("title") or ""

        # 구글 뉴스 제목 끝에 붙는 ' - 언론사' 제거
        if title.endswith(" - " + source):
            title = title[: -len(" - " + source)]

        try:
            date = parsedate_to_datetime(item.findtext("pubDate")).strftime("%Y-%m-%d")
        except Exception:
            date = ""

        # RSS 설명은 제목을 반복하는 경우가 많아 다를 때만 사용
        desc = strip_html(item.findtext("description") or "").strip()
        if title in desc:
            desc = ""

        news_list.append({
            "title": title,
            "description": desc,
            "source": source,
            "date": date,
            "link": item.findtext("link") or ""
        })

        if len(news_list) >= 10:
            break

    return news_list


def search_news(query):
    """GNews API 뉴스 검색"""

    import requests

    api_key = get_gnews_key()

    # [쇼츠 추가] 키가 없으면 구글 뉴스 RSS 로 대신 검색
    if not api_key:
        st.info("GNews API 키가 없어 구글 뉴스 RSS 로 검색합니다.")
        return search_news_rss(query)

    url = "https://gnews.io/api/v4/search"

    params = {
        "q": query,
        "max": 10,
        "sortby": "publishedAt",
        "apikey": api_key
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        if response.status_code != 200:
            try:
                error_data = response.json()
                error_message = error_data.get(
                    "errors",
                    error_data.get("message", "알 수 없는 오류")
                )
            except ValueError:
                error_message = "서버 응답을 확인할 수 없습니다."

            st.error(
                f"GNews API 오류: {response.status_code}"
            )
            st.warning(f"오류 내용: {error_message}")
            st.info("구글 뉴스 RSS 로 대신 검색합니다.")  # [쇼츠 추가]
            return search_news_rss(query)

        data = response.json()
        news_list = []

        for article in data.get("articles", []):
            source = article.get("source") or {}

            news_list.append({
                "title": article.get("title") or "",
                "description": article.get("description") or "",
                "source": source.get("name") or "출처 미상",
                "date": (article.get("publishedAt") or "")[:10],
                "link": article.get("url") or ""
            })

        return news_list

    except requests.RequestException:
        st.error("GNews 서버에 연결하지 못했습니다.")
        st.info("구글 뉴스 RSS 로 대신 검색합니다.")  # [쇼츠 추가]
        return search_news_rss(query)

# ==========================================
# 관심기사 저장
# ==========================================
def save_favorite(username, news, category):
    """저장하면 True, 이미 저장된 기사면 False"""

    conn = get_db()

    try:
        exists = conn.execute(
            "SELECT 1 FROM favorites WHERE username=? AND link=?",
            (username, news["link"])
        ).fetchone()

        if exists:
            return False

        conn.execute("""
            INSERT INTO favorites
            (username, title, source, link, category, created_at)

            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            username,
            news["title"],
            news["source"],
            news["link"],
            category,
            datetime.now().strftime("%Y-%m-%d %H:%M")
        ))

        conn.commit()
        return True

    finally:
        conn.close()

def delete_favorite(username, link):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM favorites WHERE username=? AND link=?",
            (username, link)
        )
        conn.commit()
    finally:
        conn.close()

def delete_post(post_id, username):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM posts WHERE id=? AND username=?",
            (post_id, username)
        )
        conn.commit()
    finally:
        conn.close()

# ==========================================
# SESSION
# ==========================================

if "user" not in st.session_state:
    st.session_state.user = None

if "news_results" not in st.session_state:
    st.session_state.news_results = []

if "role" not in st.session_state:
    st.session_state.role = None


# ==========================================
# 사이드바
# ==========================================
st.sidebar.title("⚓ SHIP NEWS")

menu = st.sidebar.radio(
    "메뉴",
    [
        "🏠 홈",
        "📰 뉴스",
        "⭐ 관심기사",
        "🎬 쇼츠 생성",   # [쇼츠 추가]
        "💬 커뮤니티",
        "📊 뉴스 통계",
        "🛠 관리자",
        "🔐 로그인"
    ]
)


# ==========================================
# 홈
# ==========================================
if menu == "🏠 홈":

    st.title("⚓ SHIP NEWS")

    st.subheader("조선산업 뉴스 정보 플랫폼")

    st.write("""
    조선산업과 관련된 뉴스를 검색하고
    기사 분야와 출처를 자동으로 분류하는 서비스입니다.

    🎬 선택한 기사는 세로형 쇼츠 영상으로 자동 제작할 수 있습니다.
    """)

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("주요 분야", "7개")

    with col2:
        st.metric("출처 분류", "4종")

    with col3:
        st.metric(
            "로그인",
            "ON" if st.session_state.user else "OFF"
        )

    st.divider()

    st.subheader("관심 분야")

    st.write("""
    🚢 LNG선 · 컨테이너선

    💰 수주

    ⚓ 방산

    🌱 친환경 선박

    🌊 해운

    📊 기업·실적
    """)


# ==========================================
# 뉴스
# ==========================================
elif menu == "📰 뉴스":

    st.title("📰 조선산업 뉴스")

    query = st.text_input(
        "뉴스 검색",
        placeholder="예: 한화오션 LNG선",
        max_chars=100
    )

    category_filter = st.selectbox(
        "분야",
        [
            "전체",
            "수주",
            "LNG선",
            "컨테이너선",
            "방산",
            "친환경",
            "해운",
            "기업·실적"
        ]
    )

    if st.button("🔍 검색"):

        if not query.strip():
            st.warning("검색어를 입력하세요.")

        else:
            # 검색 결과를 세션에 보관해야 '저장' 버튼을 눌러 화면이
            # 다시 실행돼도 결과가 사라지지 않는다.
            st.session_state.news_results = search_news(query.strip())

    for i, news in enumerate(st.session_state.news_results):

        categories = classify_news(
            news["title"],
            news["description"]
        )

        # 필터
        if (
            category_filter != "전체"
            and category_filter not in categories
        ):
            continue

        source_category = classify_source(
            news["source"]
        )

        with st.container(border=True):

            st.subheader(esc(news["title"]))

            col1, col2, col3 = st.columns(3)

            with col1:
                st.write(
                    f"**출처:** {esc(news['source'])}"
                )

            with col2:
                st.write(
                    f"**출처 분류:** {source_category}"
                )

            with col3:
                st.write(
                    f"**날짜:** {esc(news['date'])}"
                )

            st.write(esc(news["description"]))

            st.write(
                "**분야:** "
                + " / ".join(categories)
            )

            if is_safe_url(news["link"]):
                st.link_button(
                    "🔗 원문 기사",
                    news["link"]
                )
            else:
                st.caption("안전하지 않은 링크라서 표시하지 않았습니다.")

            if st.session_state.user:

                if st.button(
                    "⭐ 관심기사 저장",
                    key=f"favorite_{i}"
                ):

                    if save_favorite(
                        st.session_state.user,
                        news,
                        ", ".join(categories)
                    ):
                        st.success(
                            "관심기사에 저장했습니다."
                        )
                    else:
                        st.info(
                            "이미 저장한 기사입니다."
                        )

            else:

                st.caption(
                    "관심기사 저장은 로그인이 필요합니다."
                )


# ==========================================
# 관심기사
# ==========================================
elif menu == "⭐ 관심기사":

    st.title("⭐ 관심기사")

    if not st.session_state.user:

        st.warning("로그인이 필요합니다.")

    else:

        conn = get_db()

        favorites = conn.execute("""
            SELECT title, source, category, link, created_at

            FROM favorites

            WHERE username=?

            ORDER BY id DESC
        """, (
            st.session_state.user,
        )).fetchall()

        conn.close()

        if not favorites:

            st.info("저장한 기사가 없습니다.")

        for article in favorites:

            with st.container(border=True):

                st.subheader(esc(article[0]))

                st.write(
                    f"출처 : {esc(article[1])}"
                )

                st.write(
                    f"분야 : {esc(article[2])}"
                )

                st.write(
                    f"저장 : {esc(article[4])}"
                )

                if is_safe_url(article[3]):
                    st.link_button(
                        "원문",
                        article[3]
                    )

                if st.button("⭐ 저장 취소", key=f"unfavorite_{article[3]}"):
                    delete_favorite(st.session_state.user, article[3])
                    st.success("관심기사 저장을 취소했습니다.")
                    st.rerun()


# ==========================================
# [쇼츠 추가] 쇼츠 생성
# ==========================================
elif menu == "🎬 쇼츠 생성":

    import shorts

    st.title("🎬 뉴스 쇼츠 생성")
    st.write("검색한 뉴스나 관심기사를 골라 세로형(9:16) 쇼츠 영상을 자동으로 만듭니다. "
             "파도와 배가 움직이는 배경 위에 제목·자막 애니메이션, 음성, 배경음악이 들어갑니다.")

    source_type = st.radio(
        "기사 가져오기",
        ["현재 검색 결과", "관심기사"],
        horizontal=True
    )

    candidates = []

    if source_type == "현재 검색 결과":
        for news in st.session_state.news_results:
            candidates.append({
                **news,
                "category": ", ".join(classify_news(news["title"], news["description"]))
            })
        if not candidates:
            st.info("📰 뉴스 메뉴에서 먼저 검색하세요.")

    else:
        if not st.session_state.user:
            st.warning("관심기사를 쓰려면 로그인이 필요합니다.")
        else:
            conn = get_db()
            rows = conn.execute("""
                SELECT title, source, link, category, created_at
                FROM favorites WHERE username=? ORDER BY id DESC
            """, (st.session_state.user,)).fetchall()
            conn.close()
            for r in rows:
                candidates.append({
                    "title": r[0], "source": r[1], "link": r[2],
                    "category": r[3], "date": (r[4] or "")[:10], "description": ""
                })
            if not candidates:
                st.info("저장한 관심기사가 없습니다.")

    if candidates:

        labels = [
            f"[{c['category']}] {c['title']} ({c['source']})" for c in candidates
        ]

        picked = st.multiselect(
            "쇼츠에 넣을 기사 (최대 3건)",
            options=list(range(len(candidates))),
            format_func=lambda i: labels[i],
            max_selections=3
        )

        headline = st.text_input(
            "첫 화면 제목",
            value="오늘의 조선해양 뉴스",
            max_chars=30
        )

        use_voice = st.checkbox(
            "음성 넣기 (인터넷 필요, 실패하면 자막만 있는 영상으로 제작)",
            value=True
        )

        use_bgm = st.checkbox(
            "배경음악 넣기 (직접 합성한 음악, 저작권 걱정 없음)",
            value=True
        )

        if st.button("🎬 쇼츠 만들기", disabled=not picked):

            bar = st.progress(0.0, text="준비 중")

            try:
                result = shorts.build_shorts(
                    [candidates[i] for i in picked],
                    headline=headline.strip() or "오늘의 조선해양 뉴스",
                    use_voice=use_voice,
                    use_bgm=use_bgm,
                    progress=lambda v, m: bar.progress(min(v, 1.0), text=m)
                )
                st.session_state.last_shorts = result

            except Exception as e:
                st.error(f"쇼츠를 만들지 못했습니다: {e}")

    # 마지막으로 만든 쇼츠 보여주기
    result = st.session_state.get("last_shorts")

    if result and os.path.exists(result["video"]):

        st.divider()
        st.subheader("완성된 쇼츠")

        if not result["has_voice"]:
            st.caption("음성을 만들지 못한 장면이 있어 자막 위주로 제작되었습니다.")

        col1, col2 = st.columns([1, 2])

        with col1:
            st.video(result["video"])

        with col2:
            st.write(f"**장면 수:** {result['scenes']}개")
            st.write(f"**길이:** 약 {result.get('seconds', '-')}초")
            st.write(f"**파일:** {os.path.basename(result['video'])}")
            with open(result["video"], "rb") as f:
                st.download_button(
                    "⬇️ mp4 다운로드",
                    f.read(),
                    file_name=os.path.basename(result["video"]),
                    mime="video/mp4"
                )


# ==========================================
# 커뮤니티
# ==========================================

elif menu == "💬 커뮤니티":

    st.title("💬 조선산업 커뮤니티")

    if st.session_state.user:

        with st.expander("✏️ 게시글 작성"):

            post_title = st.text_input(
                "제목",
                max_chars=MAX_POST_TITLE
            )

            post_content = st.text_area(
                "내용",
                max_chars=MAX_POST_CONTENT
            )

            if st.button("게시"):

                post_title = post_title.strip()
                post_content = post_content.strip()

                if not post_title or not post_content:

                    st.warning("제목과 내용을 입력하세요.")

                elif (
                    len(post_title) > MAX_POST_TITLE
                    or len(post_content) > MAX_POST_CONTENT
                ):

                    st.error("글이 너무 깁니다.")

                else:

                    conn = get_db()

                    conn.execute("""
                        INSERT INTO posts
                        (username, title, content, created_at)

                        VALUES (?, ?, ?, ?)
                    """, (
                        st.session_state.user,
                        post_title,
                        post_content,
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M"
                        )
                    ))

                    conn.commit()
                    conn.close()

                    st.success("게시글이 등록되었습니다.")

                    st.rerun()

    else:

        st.info(
            "글을 작성하려면 로그인하세요."
        )

    conn = get_db()

    posts = conn.execute("""
        SELECT id, username, title, content, created_at
        FROM posts
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    for post in posts:

        with st.container(border=True):

            st.subheader(esc(post[2]))

            st.caption(
                f"{esc(post[1])} | {esc(post[4])}"
            )

            st.text(post[3])

            # 작성자 본인만 삭제 가능
            if st.session_state.user == post[1]:

                if st.button(
                    "🗑️ 게시글 삭제",
                    key=f"delete_post_{post[0]}"
                ):
                    delete_post(
                        post[0],
                        st.session_state.user
                    )

                    st.success("게시글이 삭제되었습니다.")
                    st.rerun()

# ==========================================
# 뉴스 통계
# ==========================================
elif menu == "📊 뉴스 통계":
    st.title("📊 뉴스 통계")
    results = st.session_state.news_results
    if not results:
        st.info("뉴스를 먼저 검색하면 현재 검색 결과의 분류 통계를 볼 수 있습니다.")
    else:
        counts = {}
        source_counts = {}
        for news in results:
            for category in classify_news(news.get("title", ""), news.get("description", "")):
                counts[category] = counts.get(category, 0) + 1
            sc = classify_source(news.get("source", ""))
            source_counts[sc] = source_counts.get(sc, 0) + 1
        st.subheader("분야별 기사 수")
        st.bar_chart(counts)
        st.subheader("출처 유형별 기사 수")
        st.bar_chart(source_counts)


# ==========================================
# 관리자
# ==========================================
elif menu == "🛠 관리자":
    st.title("🛠 관리자")
    if not st.session_state.user or st.session_state.role != "admin":
        st.warning("관리자 계정만 접근할 수 있습니다.")
    else:
        conn = get_db()
        users = conn.execute("SELECT id, username, role FROM users ORDER BY id DESC").fetchall()
        posts = conn.execute("SELECT id, username, title, created_at FROM posts ORDER BY id DESC").fetchall()
        conn.close()
        st.metric("회원 수", len(users))
        st.metric("게시글 수", len(posts))
        st.subheader("회원")
        st.dataframe([{"ID": u[0], "아이디": u[1], "권한": u[2]} for u in users], use_container_width=True)
        st.subheader("게시글 관리")
        for post in posts:
            c1, c2 = st.columns([5, 1])
            with c1:
                st.write(f"#{post[0]} {esc(post[2])} — {esc(post[1])} / {esc(post[3])}")
            with c2:
                if st.button("삭제", key=f"admin_delete_{post[0]}"):
                    conn = get_db()
                    conn.execute("DELETE FROM posts WHERE id=?", (post[0],))
                    conn.commit()
                    conn.close()
                    st.rerun()


# ==========================================
# 로그인 / 회원가입
# ==========================================
elif menu == "🔐 로그인":

    st.title("🔐 회원")

    if st.session_state.user:

        st.success(
            f"{st.session_state.user}님 로그인 중"
        )

        if st.button("로그아웃"):

            st.session_state.user = None
            st.session_state.role = None
            st.rerun()

    else:

        login_tab, register_tab = st.tabs(
            ["로그인", "회원가입"]
        )

        # 로그인
        with login_tab:

            username = st.text_input(
                "아이디",
                key="login_id",
                max_chars=20
            )

            password = st.text_input(
                "비밀번호",
                type="password",
                key="login_pw",
                max_chars=MAX_PASSWORD_LEN
            )

            if st.button("로그인"):

                user, error = login(
                    username,
                    password
                )

                if user:

                    st.session_state.user = user[0]
                    st.session_state.role = user[1]

                    st.success(
                        "로그인되었습니다."
                    )

                    st.rerun()

                else:

                    st.error(error)

        # 회원가입
        with register_tab:

            new_id = st.text_input(
                "새 아이디",
                max_chars=20
            )

            new_pw = st.text_input(
                "새 비밀번호",
                type="password",
                max_chars=MAX_PASSWORD_LEN
            )

            new_pw2 = st.text_input(
                "비밀번호 확인",
                type="password",
                max_chars=MAX_PASSWORD_LEN
            )

            if st.button("회원가입"):

                new_id = new_id.strip()

                id_error = validate_username(new_id)
                pw_error = validate_password(new_pw)

                if id_error:

                    st.warning(id_error)

                elif pw_error:

                    st.warning(pw_error)

                elif new_pw != new_pw2:

                    st.error(
                        "비밀번호가 일치하지 않습니다."
                    )

                elif register(
                    new_id,
                    new_pw
                ):

                    st.success(
                        "회원가입 완료!"
                    )

                else:

                    st.error(
                        "이미 존재하는 아이디입니다."
                    )