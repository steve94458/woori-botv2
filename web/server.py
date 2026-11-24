"""
우리거래소 — 우리봇v2 주식/채굴 웹사이트

봇과 같은 라즈베리파이에서 돌린다.
봇 폴더의 tools.py 와 bot.db 를 그대로 가져다 쓰므로
웹에서 한 거래와 디스코드에서 한 거래가 완전히 같은 규칙을 따른다.

실행:
    cd web
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import os
import sys
import time
import secrets
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode
import math
import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware


# ============================================================
# 설정 — 환경변수 또는 web/.env 에서 읽는다
# ============================================================

def _env파일_읽기():
    경로 = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(경로):
        return
    with open(경로, encoding="utf-8") as f:
        for 줄 in f:
            줄 = 줄.strip()
            if not 줄 or 줄.startswith("#") or "=" not in 줄:
                continue
            키, 값 = 줄.split("=", 1)
            os.environ.setdefault(키.strip(), 값.strip().strip('"').strip("'"))


_env파일_읽기()

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:8000/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# 비어 있으면 web 폴더의 한 칸 위(봇 폴더)를 쓴다
BOT_DIR = (os.environ.get("BOT_DIR") or "").strip() or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "0") == "1"

# nginx 등에서 하위 경로(예: /stock)로 서비스할 때 설정한다. 루트면 비워둔다.
BASE_PATH = os.environ.get("BASE_PATH", "").strip().rstrip("/")
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH

if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("[경고] SESSION_SECRET 이 없어 임시 값을 씁니다. 서버를 재시작하면 모두 로그아웃됩니다.")

if not CLIENT_ID or not CLIENT_SECRET:
    print("[경고] DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET 이 없습니다. 로그인이 동작하지 않습니다.")


# ============================================================
# 봇의 tools.py 불러오기
# ============================================================

if not os.path.exists(os.path.join(BOT_DIR, "tools.py")):
    raise SystemExit(
        f"[오류] {BOT_DIR} 에 tools.py 가 없습니다.\n"
        f"       tools.py 가 봇 폴더에 있는지, .env 의 BOT_DIR 이 맞는지 확인하세요."
    )
sys.path.insert(0, BOT_DIR)
import tools  # noqa: E402
import 주식  # noqa: E402  (장 시간·캔들·레버리지·예약 주문·배당·ETF. 주가 계산·체결·청산·지급은 봇 루프가 한다)

# tools 는 연결 하나를 공유하므로 요청이 동시에 DB를 만지지 않게 잠근다
_db락 = threading.Lock()


def db(함수, *인자, **키워드):
    with _db락:
        return 함수(*인자, **키워드)


def _조회(sql, 파라미터=()):
    with _db락:
        tools.cursor.execute(sql, 파라미터)
        return tools.cursor.fetchall()


def _실행(sql, 파라미터=()):
    with _db락:
        tools.cursor.execute(sql, 파라미터)
        tools.conn.commit()


# ============================================================
# 앱
# ============================================================

app = FastAPI(title="우리거래소", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="uri_session",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=HTTPS_ONLY,
    path=(BASE_PATH or "") + "/",
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _로그인유저(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _쓰기요청_확인(request: Request):
    """다른 사이트에서 몰래 거래를 보내는 것(CSRF)을 막는다."""
    if request.headers.get("x-requested-with") != "uri-web":
        raise HTTPException(status_code=403, detail="잘못된 요청입니다.")


def _점검확인(시스템, uid):
    """꺼져 있으면 막는다. 관리자는 통과."""
    with _db락:
        if tools.시스템켜짐(시스템):
            return
        try:
            import RPG
            if RPG.관리자인가(uid):
                return
        except Exception:
            pass
    이름 = "경제 시스템(주식·채굴)" if 시스템 == "경제" else "RPG"
    raise HTTPException(status_code=503, detail=f"🔧 지금 {이름}이 점검 중이에요. 잠시 후에 다시 시도해주세요.")


def _시스템상태():
    with _db락:
        return {"economy": tools.시스템켜짐("경제"), "rpg": tools.시스템켜짐("RPG")}


_마지막요청 = {}


def _속도제한(user_id, 간격=0.7):
    지금 = time.monotonic()
    if 지금 - _마지막요청.get(user_id, 0) < 간격:
        raise HTTPException(status_code=429, detail="너무 빠르게 요청하고 있습니다.")
    _마지막요청[user_id] = 지금


def _유저등록(user_id, 이름):
    """디스코드로 처음 들어온 사람도 봇 DB에 자리를 만든다."""
    with _db락:
        tools.cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, money) VALUES (?, '0')", (user_id,)
        )
        tools.cursor.execute(
            "UPDATE users SET name = ? WHERE user_id = ? AND (name IS NULL OR name = '익명')",
            (이름, user_id),
        )
        tools.conn.commit()


# ============================================================
# 페이지 / 로그인
# ============================================================

@app.get("/")
def 첫화면():
    # 화면 파일을 고쳤을 때 폰 브라우저가 옛날 것을 붙잡고 있지 않게 한다
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/login")
def 로그인(request: Request):
    상태값 = secrets.token_urlsafe(16)
    request.session["oauth_state"] = 상태값
    주소 = "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": 상태값,
        "prompt": "none",
    })
    return RedirectResponse(주소)


@app.get("/callback")
def 로그인완료(request: Request, code: str = "", state: str = ""):
    if not code or state != request.session.pop("oauth_state", None):
        return RedirectResponse(f"{BASE_PATH}/?error=login")

    try:
        토큰 = httpx.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        토큰.raise_for_status()
        접근토큰 = 토큰.json()["access_token"]

        내정보 = httpx.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {접근토큰}"},
            timeout=10,
        )
        내정보.raise_for_status()
        u = 내정보.json()
    except Exception as e:
        print(f"[{datetime.now()}] 디스코드 로그인 실패: {e}")
        return RedirectResponse(f"{BASE_PATH}/?error=login")

    아바타 = (
        f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=64"
        if u.get("avatar")
        else f"https://cdn.discordapp.com/embed/avatars/{(int(u['id']) >> 22) % 6}.png"
    )
    이름 = u.get("global_name") or u.get("username") or "익명"

    request.session["user"] = {"id": u["id"], "name": 이름, "avatar": 아바타}
    _유저등록(u["id"], 이름)
    print(f"[{datetime.now()}] 웹 로그인: {이름} ({u['id']})")
    return RedirectResponse(f"{BASE_PATH}/")


@app.get("/logout")
def 로그아웃(request: Request):
    request.session.clear()
    return RedirectResponse(f"{BASE_PATH}/")


# ============================================================
# 업비트 — 코인 그래프와 등락률을 업비트에서 바로 가져온다
# ============================================================

업비트 = "https://api.upbit.com/v1"
_업비트캐시 = {}          # 키: (종류, 인자) → (저장시각, 값)
_캐시락 = threading.Lock()

# 기간별 캔들: (업비트 캔들 종류, 개수)
_캔들설정 = {"1h": ("minutes/1", 60), "1d": ("minutes/10", 144), "1w": ("minutes/60", 168), "all": ("days", 200)}


def _캐시(키, 유효초, 가져오기):
    지금 = time.monotonic()
    with _캐시락:
        저장 = _업비트캐시.get(키)
        if 저장 and 지금 - 저장[0] < 유효초:
            return 저장[1]
    try:
        값 = 가져오기()
    except Exception as e:
        print(f"[{datetime.now()}] 업비트 조회 실패 {키}: {e}")
        return 저장[1] if 저장 else None      # 실패하면 예전 값이라도 쓴다
    with _캐시락:
        _업비트캐시[키] = (지금, 값)
    return 값


def _업비트캔들(마켓, 구간):
    종류, 개수 = _캔들설정.get(구간, _캔들설정["1d"])

    def 가져오기():
        r = httpx.get(
            f"{업비트}/candles/{종류}",
            params={"market": 마켓, "count": 개수},
            headers={"Accept": "application/json"},
            timeout=5,
        )
        r.raise_for_status()
        # 최신이 먼저 오므로 뒤집는다
        원 = lambda v: int(round(float(v)))       # noqa: E731
        return [
            {"t": c["candle_date_time_kst"], "o": 원(c["opening_price"]), "h": 원(c["high_price"]),
             "l": 원(c["low_price"]), "c": 원(c["trade_price"])}
            for c in reversed(r.json())
        ]

    return _캐시(("캔들", 마켓, 구간), 60, 가져오기)


def _업비트등락률(마켓들):
    """전일 종가 대비 등락률 {마켓: 비율}. 한 번에 모든 코인을 가져온다."""
    if not 마켓들:
        return {}
    키 = ("등락", ",".join(sorted(마켓들)))

    def 가져오기():
        r = httpx.get(
            f"{업비트}/ticker",
            params={"markets": ",".join(sorted(마켓들))},
            headers={"Accept": "application/json"},
            timeout=5,
        )
        r.raise_for_status()
        return {t["market"]: float(t.get("signed_change_rate") or 0) for t in r.json()}

    return _캐시(키, 30, 가져오기) or {}


def _코인마켓맵():
    """{심볼: 업비트 마켓}"""
    return {심볼: 마켓 for 심볼, 마켓 in _조회(
        "SELECT symbol, api_market FROM stocks WHERE is_premium = 2 AND api_market IS NOT NULL"
    )}


# ============================================================
# 조회 API
# ============================================================

@app.get("/api/me")
def 내정보(request: Request):
    user = request.session.get("user")
    if not user:
        return {"loggedIn": False}

    uid = user["id"]
    현금 = db(tools._get_money, uid) or 0
    사토시 = db(tools._사토시잔액, uid)
    return {
        "loggedIn": True,
        "systems": _시스템상태(),
        "id": uid,
        "name": user["name"],
        "avatar": user["avatar"],
        "cash": 현금,
        "satoshi": 사토시,
    }


@app.get("/api/stocks")
def 종목목록():
    심볼들 = [r[0] for r in _조회(
        "SELECT symbol FROM stocks ORDER BY is_premium DESC, CAST(price AS INTEGER) DESC"
    )]
    코인마켓 = _코인마켓맵()
    코인등락 = _업비트등락률(list(코인마켓.values()))
    전일 = db(주식.전일종가들, tools.cursor)
    정지 = db(주식.정지목록, tools.cursor)
    거래량 = db(주식.오늘거래량, tools.cursor)
    배당 = {x["symbol"]: x for x in db(주식.배당표)}
    다음배당 = 주식.다음배당일()
    ETF요약 = {x["symbol"]: x for x in db(주식.요약, tools.cursor)}
    지금 = datetime.now()

    결과 = []
    for 심볼 in 심볼들:
        정보 = db(tools.시장정보, 심볼)
        if not 정보:
            continue
        마켓 = 코인마켓.get(심볼)
        if 마켓 and 마켓 in 코인등락:
            등락 = 코인등락[마켓]
        else:
            이전 = 전일.get(심볼)
            등락 = (정보["price"] - 이전) / 이전 if 이전 else 0
        결과.append({
            "symbol": 심볼,
            "name": 정보["name"],
            "market": 정보["market"],
            "marketName": tools.시장_이름.get(정보["market"], "?"),
            "sector": 정보.get("sector"),
            "price": 정보["price"],
            "change": 등락,
            "changeBasis": "전일 대비",
            "supply": 정보["supply"],
            "remain": 정보["remain"],
            "held": 정보["held"],
            "limit": tools.지분한도(정보),
            "stale": bool(db(tools.코인시세_확인, 정보)) if 정보["market"] == 2 else False,
            **(_제도정보(심볼, 정지, 거래량, 배당, 다음배당, 지금, bool(정보.get("etf"))) if 정보["market"] != 2 else {}),
            "etf": _ETF칸(정보, ETF요약.get(심볼)),
        })
    return 결과


def _ETF칸(정보, 요약):
    e = 정보.get("etf")
    if not e:
        return None
    return {"lev": e["lev"], "levName": 주식.배율이름(e["lev"]), "index": 주식.지수이름(e["rule"]),
            "fee": e["fee"], "desc": e["desc"], "count": (요약 or {}).get("count", 0)}


def _정지글(v):
    return {"kind": v["종류"], "until": v["끝"], "left": v["남은초"], "text": v["글"]} if v else None


def _제도정보(심볼, 정지, 거래량, 배당, 다음배당, 지금, etf=False):
    실적 = None if etf else 주식.다음실적(심볼, 지금)        # ETF 는 실적 발표가 없다
    x = 배당.get(심볼) or {}
    v, 대금 = 거래량.get(심볼, (0, 0))
    return {
        "halt": _정지글(정지.get(심볼)),
        "volume": round(v), "turnover": 대금,
        "dividendYield": x.get("yield", 0), "dividendPerShare": x.get("dps", 0),
        "nextDividend": f"{다음배당:%Y-%m-%d}" if 다음배당 and x.get("yield") else None,
        "nextEarnings": f"{실적:%Y-%m-%d}" if 실적 else None, "earningsToday": bool(실적) and 실적.date() == 지금.date(),
    }


@app.get("/api/etf/{symbol}")
def ETF상세(symbol: str):
    d = db(주식.상세, tools.cursor, symbol.upper())
    if d is None:
        raise HTTPException(404, "ETF 가 아닙니다.")
    d["yield"] = db(주식.배당률, d["symbol"]) or 0
    return d


def _봉(b):
    return {"t": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4], "v": round(b[5], 1) if len(b) > 5 and b[5] else 0}


@app.get("/api/stocks/{symbol}/history")
def 가격기록조회(symbol: str, 구간: str = Query("1d", alias="range")):
    """캔들 [{t, o, h, l, c}]. range: 1h(1분봉) 1d(5분봉) 1w(1시간봉) all(일봉). t 는 한국 시각."""
    심볼 = symbol.upper()
    if 구간 not in _캔들설정:
        구간 = "1d"

    # 코인은 업비트의 실제 과거 시세로 그린다
    마켓 = _코인마켓맵().get(심볼)
    if 마켓:
        캔들 = list(_업비트캔들(마켓, 구간) or [])
        정보 = db(tools.시장정보, 심볼)
        if 캔들 and 정보:                      # 마지막 봉은 지금 가격으로 맞춘다
            끝, 가격 = dict(캔들[-1]), 정보["price"]
            끝["c"], 끝["h"], 끝["l"] = 가격, max(끝["h"], 가격), min(끝["l"], 가격)
            캔들[-1] = 끝
        return 캔들

    return [_봉(b) for b in db(주식.캔들조회, tools.cursor, 심볼, 구간)]


@app.get("/api/live")
def 실시간(symbol: str = "", 구간: str = Query("1d", alias="range")):
    """
    화면이 몇 초마다 부르는 가벼운 시세.
    market: 장 상태, prices: {심볼: [현재가, 등락률]}, candle: 보고 있는 종목의 마지막 봉 (코인은 None)
    """
    with _db락:
        tools.cursor.execute("SELECT symbol, price, is_premium, api_market FROM stocks")
        행들 = tools.cursor.fetchall()
        전일 = 주식.전일종가들(tools.cursor)
        봉 = 주식.마지막캔들(tools.cursor, symbol.upper(), 구간 if 구간 in _캔들설정 else "1d") if symbol else None
        정지 = 주식.정지목록(tools.cursor)
    코인등락 = _업비트등락률([m for _, _, 시장, m in 행들 if 시장 == 2 and m])

    가격들 = {}
    코인 = False
    for 심볼, 가격, 시장, 마켓 in 행들:
        가격 = int(float(가격))
        if 시장 == 2:
            등락 = 코인등락.get(마켓, 0)
            코인 = 코인 or 심볼 == symbol.upper()
        else:
            이전 = 전일.get(심볼)
            등락 = (가격 - 이전) / 이전 if 이전 else 0
        가격들[심볼] = [가격, 등락]
    return {"market": 주식.장상태(), "prices": 가격들, "candle": _봉(봉) if 봉 and not 코인 else None,
            "halts": {k: _정지글(v) for k, v in 정지.items()}}


@app.get("/api/stocks/{symbol}/holders")
def 지분조회(symbol: str, request: Request):
    심볼 = symbol
    정보 = db(tools.시장정보, 심볼)
    if not 정보:
        raise HTTPException(404, "없는 종목입니다.")

    행 = _조회(
        "SELECT p.user_id, COALESCE(u.name, '익명'), CAST(p.quantity AS INTEGER) "
        "FROM portfolio p LEFT JOIN users u ON u.user_id = p.user_id "
        "WHERE p.symbol = ? AND CAST(p.quantity AS INTEGER) > 0 "
        "ORDER BY CAST(p.quantity AS INTEGER) DESC",
        (정보["symbol"],),
    )
    내아이디 = (request.session.get("user") or {}).get("id")

    조각 = []
    for uid, 이름, 수량 in 행[:6]:
        조각.append({"label": 이름, "value": 수량, "me": uid == 내아이디})
    기타 = sum(r[2] for r in 행[6:])
    if 기타:
        조각.append({"label": f"그 외 {len(행) - 6}명", "value": 기타, "me": False})
    if 정보["remain"]:
        조각.append({"label": "남은 물량", "value": 정보["remain"], "me": False, "free": True})

    return {
        "symbol": 정보["symbol"],
        "name": 정보["name"],
        "total": 정보["supply"] or sum(r[2] for r in 행),
        "holders": len(행),
        "slices": 조각,
    }


@app.get("/api/portfolio")
def 내포트폴리오(request: Request):
    uid = _로그인유저(request)["id"]
    행 = _조회(
        "SELECT symbol, quantity, buyed_val FROM portfolio "
        "WHERE user_id = ? AND CAST(quantity AS INTEGER) > 0",
        (uid,),
    )
    결과 = []
    for 심볼, 수량, 매입 in 행:
        정보 = db(tools.시장정보, 심볼)
        if not 정보:
            continue
        수량, 매입 = int(float(수량)), int(float(매입 or 0))
        with _db락:
            매도평균, 평가 = tools.청산가치(정보, 수량)   # 지금 전부 팔면 실제로 받는 돈
        결과.append({
            "symbol": 심볼,
            "name": 정보["name"],
            "market": 정보["market"],
            "qty": 수량,
            "avg": 매입 // 수량 if 수량 else 0,
            "price": 정보["price"],
            "sellAvg": 매도평균,
            "value": 평가,
            "profit": 평가 - 매입,
            "rate": (평가 - 매입) / 매입 if 매입 else 0,
        })
    return 결과


@app.get("/api/mining")
def 채굴현황(request: Request):
    uid = _로그인유저(request)["id"]
    with _db락:
        목록 = tools._채굴기_목록()
        보유 = tools._보유장비(uid)
        사토시, 누적, 정전, 누적wh, 미납, 마지막청구 = tools._채굴정보(uid)

    장비 = []
    총해시 = 총와트 = 0
    for 코드, 수량 in 보유.items():
        if 코드 not in 목록:
            continue
        이름, 가격, 해시, 설명, 와트 = 목록[코드]
        총해시 += 해시 * 수량
        총와트 += 와트 * 수량
        장비.append({"code": 코드, "name": 이름, "qty": 수량, "hash": 해시, "watt": 와트})

    상점 = [
        {
            "code": 코드, "name": 이름, "price": 가격, "hash": 해시,
            "watt": 와트, "desc": 설명,
            "dailySat": 해시 * 144,
            "dailyBill": round(와트 / 1000 * 24 * tools.전기요금_kwh),
        }
        for 코드, (이름, 가격, 해시, 설명, 와트) in 목록.items()
    ]

    다음청구 = None
    if 마지막청구:
        다음청구 = (마지막청구 + timedelta(hours=tools.청구_주기_시간)).isoformat(timespec="minutes")

    return {
        "satoshi": 사토시,
        "totalMined": 누적,
        "rigs": 장비,
        "rigCount": sum(보유.values()),
        "rigLimit": tools.최대_채굴기,
        "hashPer10m": 총해시,
        "watt": 총와트,
        "dailyBill": round(총와트 / 1000 * 24 * tools.전기요금_kwh),
        "pendingBill": tools._요금계산(누적wh),
        "unpaid": 미납,
        "blackoutUntil": 정전.isoformat(timespec="minutes") if 정전 else None,
        "nextBill": 다음청구,
        "shop": 상점,
        "refundRate": tools.채굴기_환급률,
    }



@app.get("/api/quote")
def 견적(request: Request, symbol: str, side: str = "buy", qty: str = "1"):
    """실제로 거래하지 않고 예상 체결가를 계산한다."""
    user = request.session.get("user")
    with _db락:
        정보 = tools.시장정보(symbol)
        if not 정보:
            raise HTTPException(404, "없는 종목입니다.")
        코인 = 정보["market"] == 2
        단위 = "sat" if (코인 and side == "buy") else "원"
        if not 코인 and not 주식.장중():
            상태 = 주식.장상태()
            return {"ok": False, "message": f"{상태['text']} · 주식은 장중({상태['hours']})에만 거래할 수 있어요"}
        정지글 = None if 코인 else 주식.거래정지(tools.cursor, 정보["symbol"])
        if 정지글:
            return {"ok": False, "message": 정지글}

        if qty.strip() in tools._전부:
            if not user:
                return {"ok": False, "message": "로그인하면 최대 수량을 계산해드려요."}
            if side == "buy":
                예산 = tools._사토시잔액(user["id"]) if 코인 else (tools._get_money(user["id"]) or 0)
                가능 = None if 코인 else tools.추가매수가능(정보, user["id"])
                개수 = tools._최대매수수량(정보, 예산, 가능)
            else:
                tools.cursor.execute(
                    "SELECT quantity FROM portfolio WHERE user_id = ? AND symbol = ?",
                    (user["id"], 정보["symbol"]),
                )
                행 = tools.cursor.fetchone()
                개수 = int(float(행[0])) if 행 else 0
        else:
            try:
                개수 = int(qty.replace(",", ""))
            except ValueError:
                return {"ok": False, "message": "수량은 숫자로 입력해주세요."}

        if 개수 <= 0:
            return {"ok": False, "message": "거래할 수 있는 수량이 없습니다."}
        if side == "buy" and 정보["remain"] is not None and 개수 > 정보["remain"]:
            return {"ok": False, "message": f"{정보['remain']:,}주만 남아 있습니다."}
        if side == "buy" and user and not 코인:
            가능 = tools.추가매수가능(정보, user["id"])
            if 가능 is not None and 개수 > 가능:
                return {"ok": False, "message": (
                    f"1인 최대 {tools.지분한도(정보):,}주까지라 {가능:,}주만 더 살 수 있어요."
                )}

        체결가, 후, _ = tools._예상체결(정보, 개수, side == "buy")

    원금 = 체결가 * 개수
    수수료 = 0 if (코인 and side == "buy") else int(원금 * tools.거래_수수료)
    거래세 = int(원금 * tools.거래세율(정보)) if side == "sell" else 0
    return {
        "ok": True,
        "qty": 개수,
        "fill": 체결가,
        "total": 원금 + 수수료 if side == "buy" else 원금 - 수수료 - 거래세,
        "fee": 수수료,
        "tax": 거래세,
        "after": 후,
        "unit": 단위,
    }


# ============================================================
# 거래 API
# ============================================================

@app.post("/api/trade")
async def 거래(request: Request):
    _쓰기요청_확인(request)
    uid = _로그인유저(request)["id"]
    _점검확인("경제", uid)
    _속도제한(uid)

    몸 = await request.json()
    심볼 = str(몸.get("symbol", "")).strip()
    방향 = 몸.get("side")
    수량 = str(몸.get("qty", "")).strip()

    if 방향 not in ("buy", "sell") or not 심볼 or not 수량:
        raise HTTPException(400, "종목, 매수/매도, 수량을 모두 정해주세요.")

    함수 = tools.매수실행 if 방향 == "buy" else tools.매도실행
    성공, 결과 = db(함수, uid, 심볼, 수량)
    if not 성공:
        return JSONResponse({"ok": False, "message": 결과}, status_code=400)

    print(f"[{datetime.now()}] 웹 {'매수' if 방향 == 'buy' else '매도'}: {uid} {심볼} {수량}")
    return {"ok": True, "result": 결과}


@app.get("/api/orders")
def 내주문(request: Request):
    uid = _로그인유저(request)["id"]
    with _db락:
        열린 = 주식.주문목록(uid)
        끝난 = 주식.주문목록(uid, 열린것=False, 개수=10)
    return {"open": 열린, "done": 끝난, "max": 주식.주문_최대, "days": 주식.주문_유효일}


@app.post("/api/orders")
async def 주문하기(request: Request):
    _쓰기요청_확인(request)
    uid = _로그인유저(request)["id"]
    _점검확인("경제", uid)
    _속도제한(uid)
    몸 = await request.json()
    if 몸.get("action") == "cancel":
        성공, 결과 = db(주식.주문취소, uid, str(몸.get("id", "")))
    else:
        성공, 결과 = db(주식.주문넣기, uid, str(몸.get("kind", "")), str(몸.get("symbol", "")),
                       str(몸.get("price", "")), str(몸.get("qty", "")), None)
    if not 성공:
        return JSONResponse({"ok": False, "message": 결과}, status_code=400)
    print(f"[{datetime.now()}] 웹 주문 {몸.get('action', 'place')}: {uid} {몸}")
    return {"ok": True, "result": 결과}


@app.get("/api/tax")
def 내세금(request: Request):
    uid = _로그인유저(request)["id"]
    with _db락:
        세금 = tools.세금조회(uid)
        세전, 세 = 주식.예상배당(uid)
        받은 = 주식.내배당(uid, 10)
    다음 = 주식.다음배당일()
    return {**세금, "nextDividend": f"{다음:%Y-%m-%d}" if 다음 else None, "expectedDividend": 세전, "expectedDividendTax": 세,
            "received": [{"date": a, "name": b, "symbol": c, "qty": d, "gross": e, "tax": f} for a, b, c, d, e, f in 받은]}


@app.get("/api/calendar")
def 일정():
    지금 = datetime.now()
    이름들 = dict(_조회(f"SELECT symbol, name FROM stocks WHERE is_premium IN (0, 1) AND {주식.아님}"))
    목록 = 주식.실적일정(list(이름들), 21, 지금)
    다음 = 주식.다음배당일()
    return {
        "earnings": [{"date": f"{d:%Y-%m-%d}", "symbol": s, "name": 이름들[s]} for d, s in 목록],
        "seasons": [list(x) for x in 주식.실적_시즌],
        "nextDividend": f"{다음:%Y-%m-%d}" if 다음 else None,
        "dividendMonths": list(주식.배당월),
        "rules": {"vi": 주식.VI_기준, "viMinutes": 주식.VI_분, "circuit": [list(x) for x in 주식.서킷_단계],
                  "tradeTax": tools.증권거래세, "cgtRate": tools.양도세율, "cgtDeduction": tools.양도세_공제,
                  "dividendTax": tools.배당세율, "fee": tools.거래_수수료},
    }


@app.get("/api/leverage")
def 내레버리지(request: Request):
    uid = _로그인유저(request)["id"]
    with _db락:
        목록 = 주식.목록(uid)
        기록 = 주식.기록(uid, 10)
    키 = ("id", "symbol", "name", "side", "lev", "margin", "price", "move", "value", "profit", "rate", "liq", "opened_at",
         "auto_refill", "added", "refills", "invested")
    return {
        "positions": [{k: p[k] for k in 키} for p in 목록],
        "history": 기록,
        "levels": list(주식.배율들), "fee": 주식.수수료,
        "minMargin": 주식.최소증거금, "maxMargin": 주식.최대증거금, "maxPositions": 주식.최대포지션,
        "loan": {"max": tools.최대_대출금, "rate": tools.대출_이자율, "days": tools.대출_기한_일},
    }


@app.post("/api/leverage/{action}")
async def 레버리지거래(action: str, request: Request):
    _쓰기요청_확인(request)
    uid = _로그인유저(request)["id"]
    _점검확인("경제", uid)
    _속도제한(uid)
    몸 = await request.json()
    if action == "open":
        성공, 결과 = db(주식.열기, uid, str(몸.get("symbol", "")), str(몸.get("side", "")),
                       str(몸.get("lev", "")), str(몸.get("margin", "")).strip(), None, 몸.get("auto") is True)
    elif action == "close":
        성공, 결과 = db(주식.닫기, uid, str(몸.get("id", "")))
    else:
        raise HTTPException(404, "없는 동작입니다.")
    if not 성공:
        return JSONResponse({"ok": False, "message": 결과}, status_code=400)
    print(f"[{datetime.now()}] 웹 레버리지 {action}: {uid} {몸}")
    return {"ok": True, "result": 결과}


@app.post("/api/mining/{action}")
async def 채굴조작(action: str, request: Request):
    동작 = action
    _쓰기요청_확인(request)
    uid = _로그인유저(request)["id"]
    _점검확인("경제", uid)
    _속도제한(uid)
    몸 = await request.json() if (await request.body()) else {}

    if 동작 not in ("buy", "sell", "pay"):
        raise HTTPException(404, "없는 동작입니다.")

    # tools 의 채굴 함수는 결과를 문장으로만 돌려주므로,
    # 실행 전후의 상태를 비교해서 성공 여부를 판단한다.
    with _db락:
        전장비 = sum(tools._보유장비(uid).values())
        전미납 = tools._채굴정보(uid)[4]

        if 동작 == "buy":
            메시지 = tools.채굴기구매(uid, 몸.get("code"), 몸.get("qty") or 1)
            성공 = sum(tools._보유장비(uid).values()) > 전장비
        elif 동작 == "sell":
            메시지 = tools.채굴기판매(uid, 몸.get("code"), 몸.get("qty"))
            성공 = sum(tools._보유장비(uid).values()) < 전장비
        else:
            메시지 = tools.전기납부(uid)
            성공 = tools._채굴정보(uid)[4] < 전미납

    print(f"[{datetime.now()}] 웹 채굴 {동작}: {uid} → {메시지[:60]}")
    return JSONResponse({"ok": 성공, "message": 메시지}, status_code=200 if 성공 else 400)


# ============================================================
# RPG — 봇 폴더의 RPG.py 를 그대로 쓴다
# ============================================================

import RPG  # noqa: E402
RPG.프로그램 = "web"          # 웹에서 시작한 전투는 웹이 다시 켜질 때 되살린다
RPG.전투복원()

@app.get("/rpg")
def RPG화면():
    return FileResponse(os.path.join(STATIC_DIR, "rpg.html"), headers={"Cache-Control": "no-cache"})


def _rpg유저(request):
    u = _로그인유저(request)
    return u["id"]


def _전투정보(b):
    if b is None:
        return None
    c = b._c()          # 로그라이크는 모두 같은 캐릭터로 싸운다
    w = RPG.무기[c["weapon"]]
    탄약 = None
    if w["탄약"]:
        탄약 = {"name": w["탄약"][0], "need": w["탄약"][1], "have": RPG.보유(b.uid, "item", w["탄약"][0])}
    return {
        "floor": b.층, "state": b.상태, "log": b.로그[-10:],
        "title": (RPG.특수제목(b) if b.비밀 in RPG.특수던전 else RPG.비밀공간_제목.get(b.비밀)) if b.비밀 else None,
        "special": _특수전투정보(b, c),
        "zone": None if b.비밀 else {"name": RPG.구역이름(b.층), "desc": RPG.구역[RPG.구역키(b.층)]["설명"]},
        "raid": ({"turn": b.레이드["턴"], "limit": RPG.레이드_턴제한, "score": round(b.레이드["점수"], 1),
                  "practice": b.레이드["연습"]} if b.비밀 == "레이드" else None),
        "raidScore": round(b.레이드점수, 1) if hasattr(b, "레이드점수") else None,
        "hp": None if b.악세효과["숨김"] else b.체력, "maxHp": None if b.악세효과["숨김"] else b.최대체력,
        "danger": b.체력 < b.최대체력 * 0.15,
        "canWindow": b.상태 == "클리어" and b.층 == 50 and not b.비밀,
        "rewardAccessory": getattr(b, "보상악세", None),
        "effects": {(f"독{b.독중첩}중첩" if k == "독" else k): v for k, v in b.효과.items()}, "cooldowns": {k: v for k, v in b.쿨.items() if v},
        "fuel": b.화염충전 if c["weapon"] == "화염방사기" else None,
        "ammo": 탄약,
        "enemies": [
            {"name": e["이름"], "hp": e["hp"], "max": e["최대"], "type": e["종류"],
             "shield": e["방패"], "effects": e["효과"], "boss": e["보스"],
             "patterns": e.get("패턴", []), "aiming": e.get("조준", False), "enraged": e.get("광폭", False)}
            for e in b.살아있는적()
        ],
        "items": [
            {"name": n, "qty": q, "desc": RPG.아이템[n]["설명"]}
            for n, d in RPG.아이템.items()
            if d["전투"] and (b.상태 == "전투" or n in RPG.회복아이템) and not b.아이템불가(n)
            and (q := RPG.보유(b.uid, "item", n)) > 0
        ],
        "merchant": [
            {"name": n, "discount": 할인, "price": math.ceil(RPG.아이템[n]["price"] * (1 - 할인))}
            for n, 할인 in b.상인
        ],
        "gold": round(getattr(b, "누적골드", 0) + b.획득골드),
        "snacks": getattr(b, "누적간식", 0),
        "pet": ({"name": b.펫.get("별명", b.펫["이름"]), "emoji": RPG.펫[b.펫["이름"]]["이모지"], "hp": b.펫["hp"], "max": b.펫["최대"],
                 "weapon": b.펫["무기"],
                 "skill": RPG.펫능력문구(b.펫["이름"], b.펫["능력lv"])} if b.펫 else None),
        "kills": b.처치수,
        "newAchievements": b.새업적,
        "newLog": list(b.새로그),
    }


def _특수전투정보(b, c):
    """일일 던전·로그라이크·보스 러시 전투 중에만 붙는 정보"""
    if b.비밀 not in RPG.특수던전:
        return None
    d = b.던전
    로그라 = b.비밀 == "로그라이크"
    고를것 = d.get("선택지") or [] if b.상태 == "클리어" else []
    return {
        "mode": b.비밀,
        "result": RPG.특수결과문구(b),
        "next": {"일일": f"{d['단계'] + 1}/{RPG.일일_층수}층으로", "보스러시": "다음 보스"}.get(b.비밀),
        "weapon": c["weapon"] if 로그라 else None,
        "buffs": RPG.강화문구(b) if 로그라 else None,
        "choices": [{"emoji": RPG.로그_강화[k]["이모지"], "name": RPG.로그_강화[k]["이름"], "desc": RPG.로그_강화[k]["설명"]}
                    for k in 고를것],
        "autoRate": b.비밀 == "보스러시",      # 잡을 때 골드를 주는 모드만 자동 효율이 걸린다
    }


def _특수정보(uid):
    목록 = []
    for 모드 in RPG.특수던전:
        s = RPG.특수상태(uid, 모드)
        s["rank"] = [{"rank": i, "name": n, "score": 점수} for i, n, 점수 in s["rank"]]
        s["mine"] = {"rank": s["mine"][0], "score": s["mine"][1]} if s["mine"] else None
        목록.append(s)
    return 목록


def _레이드정보(uid):
    s = RPG.레이드상태(uid)
    s["ends"] = s["ends"].isoformat(timespec="seconds")
    s["hp"], s["max"] = round(s["hp"], 1), round(s["max"], 1)
    s["rank"] = [{"rank": i, "name": n, "score": round(d, 1), "reward": rw} for i, n, d, rw in s["rank"]]
    s["mine"] = {"rank": s["mine"][0], "score": round(s["mine"][1], 1)} if s["mine"] else None
    if s["last"]:
        l = s["last"]
        s["last"] = {"boss": l["boss"], "killed": l["killed"],
                     "top": [{"name": n, "score": round(d, 1)} for n, d, _ in l["top"]],
                     "reward": l["mine"][1] if l["mine"] else None}
    return s


def _진행전투(uid):
    """이 서버에서 진행 중인 전투. 오래 방치됐으면 정리한다."""
    b = RPG.진행중.get(uid)
    if b and b.방치됨():
        b.철수()
        return None
    return b


@app.get("/api/rpg/me")
def RPG내정보(request: Request):
    uid = _rpg유저(request)
    with _db락:
        c = RPG.캐릭터(uid)
        if c is None:
            return {"character": None}
        RPG._기본장비(uid)
        RPG.관리자혜택(uid)
        RPG.conn.commit()
        새업적 = RPG.업적확인(uid)
        c = RPG.캐릭터(uid)
        가진악세 = {a["이름"]: a["장착"] for a in RPG.내악세서리(uid)}
        s = RPG.전투능력(c)
        RPG.cursor.execute(
            "SELECT kind, name, qty, enhance FROM rpg_inventory WHERE user_id = ? AND qty > 0 ORDER BY kind, name",
            (uid,),
        )
        가방 = [{"kind": k, "name": n, "qty": q, "enhance": e} for k, n, q, e in RPG.cursor.fetchall()]
        for x in 가방:
            if x["kind"] == "weapon":
                x["display"] = RPG.무기표시(uid, x["name"])
        RPG.conn.commit()
        b = _진행전투(uid)
        다른곳전투 = b is None and RPG.전투중(uid)
        return {
            "character": {
                "name": c["name"], "level": c["level"], "exp": c["exp"], "expNeed": RPG.필요경험치(c["level"]),
                "stats": {k: c[k] for k in RPG.능력치}, "points": c["points"], "gold": c["gold"],
                "weapon": c["weapon"], "weaponDisplay": RPG.무기표시(uid, c["weapon"]), "armor": c["armor"], "best": c["best"],
            },
            "derived": {"atk": s["공격력"], "weaponDmg": round(s["무기피해"]), "hp": s["최대체력"],
                        "def": s["방어력"], "evade": s["회피"], "crit": s["치명"], "enhance": s["강화"]},
            "statMax": RPG.능력치_최대,
            "inventory": 가방,
            "battle": _전투정보(b),
            "battleElsewhere": 다른곳전투,
            "startFloor": RPG.시작층(uid),
            "autoRate": RPG.자동_효율,
            "enhanceRate": RPG.강화_골드배율,
            "snacks": RPG.간식(uid),
            "petWeapon": RPG.펫무기(uid),
            "accessoryMax": RPG.악세_최대,
            "accessories": [{"name": n, "emoji": d["이모지"], "desc": d["설명"], "hint": d.get("힌트") if n not in 가진악세 else None,
                             "owned": n in 가진악세, "equipped": 가진악세.get(n, False)}
                            for n, d in RPG.악세서리.items() if n not in RPG.황금빛정신],
            "hyper": (lambda h: {"rate": h[0], "until": h[1].isoformat(timespec="milliseconds")} if h[1] else None)(RPG.하이퍼버닝()),
            "pets": _펫정보(uid),
            "stand": _스탠드정보(uid),
            "goldRate": RPG.골드_환율,
            "cash": RPG._현금(uid),
            "newAchievements": 새업적,
            "admin": RPG.관리자인가(uid),
            "raid": _레이드정보(uid),
            "dungeons": _특수정보(uid),
            "secretRooms": ([{"key": k, "title": RPG.비밀공간_제목.get(k, k)} for k in RPG.비밀공간_바로가기]
                            if RPG.관리자인가(uid) else []),
            "systems": {"economy": tools.시스템켜짐("경제"), "rpg": tools.시스템켜짐("RPG")},
        }


def _펫정보(uid):
    가진 = {p["이름"]: p for p in RPG.내펫들(uid)}
    결과 = []
    for n, d in RPG.펫.items():
        p = 가진.get(n)
        if d.get("숨김") and not p:
            continue
        결과.append({
            "name": n, "nickname": p["별명"] if p else n, "emoji": d["이모지"], "price": d["price"], "desc": d["설명"],
            "owned": p is not None, "equipped": bool(p and p["장착"]),
            "hpLv": p["체력lv"] if p else 0, "skillLv": p["능력lv"] if p else 0,
            "maxHp": RPG.펫최대체력(n, p["체력lv"] if p else 0),
            "skill": RPG.펫능력문구(n, p["능력lv"] if p else 0),
            "hpCost": 10 * ((p["체력lv"] if p else 0) + 1), "skillCost": 10 * ((p["능력lv"] if p else 0) + 1),
        })
    return 결과


def _스탠드정보(uid):
    st = RPG.내스탠드(uid)
    return {
        "hint": RPG.스탠드디스크_힌트,
        "disks": RPG.스탠드디스크수(uid),
        "stand": ({"name": st["이름"], "level": st["레벨"], "exp": st["경험"], "next": st["다음"],
                   "slots": RPG.스탠드능력칸(st["레벨"]), "abilities": [{"name": a["이름"], "desc": RPG.스탠드능력문구(a, st["레벨"])} for a in st["능력들"]]}
                  if st else None),
        "pending": [{"id": r["id"], "kind": r["종류"], "text": r["내용"]} for r in RPG.스탠드요청목록(uid)],
        "recent": [{"id": r["id"], "kind": r["종류"], "status": r["상태"], "note": r["메모"]}
                   for r in RPG.스탠드요청목록(uid, None)[-5:] if r["상태"] != "대기"],
    }


def _친선전정보(d, uid):
    if d is None:
        return None
    if d["status"] != "active" and d["status"] != "done":
        return {"id": d["id"], "status": d["status"]}
    g = d["game"]
    return {
        "id": d["id"], "status": d["status"], "log": g.로그[-10:], "end": g.끝,
        "turn": None if g.끝 else g.지금()["uid"] == uid, "turnName": None if g.끝 else g.지금()["이름"],
        "turnNo": g.턴 + 1, "maxTurn": RPG.친선전_최대턴, "timeLimit": RPG.친선전_턴제한초,
        "winner": None if g.승자 is None else g.승자["이름"], "iWon": bool(g.승자 and g.승자["uid"] == uid),
        "players": [{"name": x["이름"], "hp": x["hp"], "max": x["최대"], "cp": x["전투력"],
                     "adj": round((x["보정"] - 1) * 100), "adjLabel": "체력" if RPG.친선전_모드 == "체력" else "공격·체력", "weapon": x["무기"], "enh": x["강화"],
                     "me": x["uid"] == uid} for x in g.p],
    }


@app.get("/api/rpg/duels")
def RPG친선전목록(request: Request):
    uid = _rpg유저(request)
    with _db락:
        목록 = RPG.내친선전(uid)
        진행 = RPG.친선전찾기(목록["active"]) if 목록["active"] else None
        마지막 = request.query_params.get("last")
        끝난것 = RPG.친선전찾기(int(마지막)) if (마지막 and 마지막.isdigit() and not 진행) else None
        if 끝난것 and uid not in (끝난것["a"], 끝난것["b"]):
            끝난것 = None
        return {
            "incoming": 목록["incoming"], "outgoing": 목록["outgoing"],
            "duel": _친선전정보(진행 or 끝난것, uid),
            "record": {"win": RPG.카운트(uid, "친선전_승"), "lose": RPG.카운트(uid, "친선전_패"), "draw": RPG.카운트(uid, "친선전_무")},
        }


@app.post("/api/rpg/duel/{action}")
async def RPG친선전(action: str, request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        if action == "challenge":
            상대 = RPG.이름으로찾기(몸.get("name", ""))
            if 상대 is None:
                return _결과(False, "그 이름의 캐릭터가 없어요.")
            성공, r = RPG.친선전신청(uid, 상대)
        elif action == "accept":
            성공, r = RPG.친선전수락(int(몸.get("id", 0)), uid)
        elif action in ("decline", "cancel"):
            성공, r = RPG.친선전거절(int(몸.get("id", 0)), uid)
        elif action == "act":
            성공, r = RPG.친선전행동(int(몸.get("id", 0)), uid, str(몸.get("type", "공격")))
        else:
            raise HTTPException(404, "없는 동작입니다.")
    return _결과(성공, r)


@app.post("/api/rpg/accessory")
async def RPG악세(request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        성공, r = RPG.악세장착(uid, 몸.get("name") or None, 해제=bool(몸.get("off")))
    return _결과(성공, r)


@app.post("/api/rpg/pet/{action}")
async def RPG펫(action: str, request: Request):
    uid, 몸 = await _rpg요청(request)
    이름 = str(몸.get("name", ""))
    with _db락:
        if action == "buy":
            성공, r = RPG.펫구매(uid, 이름)
        elif action == "equip":
            성공, r = RPG.펫장착(uid, 이름 or None)
        elif action == "upgrade":
            성공, r = RPG.펫강화(uid, 이름, 몸.get("kind", ""))
        elif action == "rename":
            성공, r = RPG.펫이름설정(uid, 이름, str(몸.get("nickname", "")))
        else:
            raise HTTPException(404, "없는 동작입니다.")
    return _결과(성공, r)


@app.post("/api/rpg/stand")
async def RPG스탠드(request: Request):
    """스탠드 요청. kind = create(name, text) / ability(text)"""
    uid, 몸 = await _rpg요청(request)
    with _db락:
        if 몸.get("kind") == "create":
            성공, r = RPG.스탠드요청(uid, "생성", str(몸.get("text", "")), str(몸.get("name", "")))
        elif 몸.get("kind") == "ability":
            성공, r = RPG.스탠드요청(uid, "능력", str(몸.get("text", "")))
        else:
            raise HTTPException(404, "없는 동작입니다.")
    return _결과(성공, {"id": r} if 성공 else r)


@app.get("/api/rpg/shop")
def RPG상점(request: Request, cat: str = "무기"):
    uid = _rpg유저(request)
    with _db락:
        결과 = []
        c = RPG.캐릭터(uid)
        if cat in ("무기", "갑옷"):
            표, 종류 = (RPG.무기, "weapon") if cat == "무기" else (RPG.갑옷, "armor")
            착용 = c["weapon"] if 종류 == "weapon" else c["armor"]
            for n, d in 표.items():
                if d.get("숨김") and not RPG.보유(uid, 종류, n):
                    continue
                됨, 이유 = RPG.해금여부(uid, 종류, n)
                결과.append({
                    "name": n, "price": d["price"], "owned": bool(RPG.보유(uid, 종류, n)),
                    "equipped": n == 착용,
                    "locked": not 됨, "reason": 이유,
                    "atk": d.get("atk"), "def": d.get("dfn"),
                    "ammo": d["탄약"][0] if 종류 == "weapon" and d["탄약"] else None,
                    "penalty": d.get("pen"), "resist": d.get("저항"),
                    "desc": d.get("설명", ""),
                    "enhance": RPG.강화수치(uid, n) if 종류 == "weapon" else 0,
                })
        else:
            for n, d in RPG.아이템.items():
                결과.append({"name": n, "price": d["price"], "have": RPG.보유(uid, "item", n),
                             "desc": d["설명"], "combat": d["전투"]})
        return 결과


@app.get("/api/rpg/achievements")
def RPG업적(request: Request):
    uid = _rpg유저(request)
    with _db락:
        새로 = RPG.업적확인(uid)
        진행 = RPG.업적진행(uid)
        return {
            "new": 새로,
            "list": [{"name": n, "desc": d["설명"], "reward": d["보상"], "rewardText": RPG.업적보상문구(n), "done": RPG.업적보유(uid, n),
                      "progress": [{"label": a, "cur": min(b, c), "goal": c} for a, b, c in 진행.get(n, [])]}
                     for n, d in RPG.업적.items()],
        }


async def _rpg요청(request):
    _쓰기요청_확인(request)
    uid = _rpg유저(request)
    _점검확인("RPG", uid)
    _속도제한(uid, 0.35)
    몸 = await request.json() if (await request.body()) else {}
    return uid, 몸


def _결과(성공, 결과):
    if not 성공:
        return JSONResponse({"ok": False, "message": 결과}, status_code=400)
    return {"ok": True, "result": 결과}


@app.post("/api/rpg/create")
async def RPG생성(request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        성공, r, 첫 = RPG.생성(uid, str(몸.get("name", "")))
    return _결과(성공, {"first": 첫} if 성공 else r)


@app.post("/api/rpg/stat")
async def RPG스탯(request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        성공, r = RPG.포인트사용(uid, 몸.get("stat"), 몸.get("n", 1))
    return _결과(성공, "ok" if 성공 else r)


@app.post("/api/rpg/gold")
async def RPG골드(request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        성공, r = RPG.골드환전(uid, "구매" if 몸.get("dir") == "buy" else "판매", 몸.get("qty", 0))
    return _결과(성공, r)


@app.post("/api/rpg/{action}")
async def RPG상점조작(action: str, request: Request):
    if action == "autoenhance":
        uid, 몸 = await _rpg요청(request)
        with _db락:
            if RPG.전투중(uid):
                return _결과(False, "전투 중에는 강화할 수 없습니다.")
            성공, r = RPG.자동강화(uid, str(몸.get("name", "")) or None, 몸.get("target"))
        return _결과(성공, r)
    if action not in ("buy", "sell", "equip", "enhance"):
        raise HTTPException(404, "없는 동작입니다.")
    uid, 몸 = await _rpg요청(request)
    이름 = str(몸.get("name", ""))
    with _db락:
        if action == "buy":
            성공, r = RPG.구매(uid, 이름, 몸.get("qty", 1))
        elif action == "sell":
            try:
                성공, r = RPG.판매(uid, 이름, 몸.get("qty", 1))
            except ValueError:
                성공, r = False, "수량이 올바르지 않습니다."
        elif action == "equip":
            성공, r = RPG.장착(uid, 이름)
        else:
            성공, r = RPG.강화(uid, 이름 or None)
    return _결과(성공, r)


@app.post("/api/rpg/dungeon/{action}")
async def RPG던전(action: str, request: Request):
    uid, 몸 = await _rpg요청(request)
    with _db락:
        b = _진행전투(uid)
        if action == "secret":
            성공, r = RPG.비밀공간바로입장(uid, str(몸.get("room", "")))
            return _결과(성공, _전투정보(r) if 성공 else r)

        if action == "raid":
            if b:
                return _결과(False, "이미 던전에 들어가 있어요. 진행 중인 전투를 마치고 오세요.")
            성공, r = RPG.레이드입장(uid)
            return _결과(성공, _전투정보(r) if 성공 else r)

        if action == "special":
            모드 = str(몸.get("mode", ""))
            if 모드 not in RPG.특수던전:
                return _결과(False, "없는 던전입니다.")
            if b:
                return _결과(False, "이미 던전에 들어가 있어요. 진행 중인 전투를 마치고 오세요.")
            성공, r = RPG.특수입장(uid, 모드)
            return _결과(성공, _전투정보(r) if 성공 else r)

        if action == "start":
            if b or RPG.전투중(uid):
                return _결과(False, "이미 던전에 들어가 있어요. 진행 중인 전투를 마치고 오세요.")
            if "petWeapon" in 몸:
                성공, r = RPG.펫무기설정(uid, 몸.get("petWeapon") or None)
                if not 성공:
                    return _결과(False, r)
            층 = RPG.시작층(uid)
            원하는층 = 몸.get("floor")
            if 원하는층:
                원하는층 = int(원하는층)
                if not (원하는층 % 10 == 1 and 원하는층 <= 층):
                    return _결과(False, f"시작할 수 있는 층은 1층, 11층 … {층}층 이에요.")
                층 = 원하는층
            b = RPG.전투(uid, 층)
            RPG.진행중[uid] = b
            return _결과(True, _전투정보(b))

        if b is None:
            return _결과(False, "진행 중인 전투가 없습니다.")

        if action == "act":
            b.행동(str(몸.get("type", "공격")), 대상=int(몸.get("target", 0) or 0), 아이템이름=몸.get("item"))
        elif action == "next":
            b.다음층()
        elif action == "pick":
            번호 = 몸.get("index")
            if not isinstance(번호, int) or not b.강화선택(번호):
                return _결과(False, "지금은 고를 수 없습니다.")
        elif action == "window":
            if not b.창밖():
                return _결과(False, "지금은 창밖으로 나갈 수 없습니다.")
        elif action == "shout":
            # 디스코드 채팅으로 외치는 것과 같다 (외침_비밀의 대사면 그 비밀공간으로)
            말 = str(몸.get("text", "")).strip()[:200]
            if not 말:
                return _결과(False, "외칠 말을 써 주세요.")
            if not b.외침(말):
                return _결과(False, f"📣 \"{말[:40]}\" … 아무 일도 일어나지 않았다.")
        elif action == "leave":
            b.철수()
            b.기록("🏳️ 던전에서 나왔다")
        elif action == "auto":
            결과 = b.자동행동(풀오토=bool(몸.get("full")))
            정보 = _전투정보(b)
            정보["autoStop"] = 결과[3:] if 결과.startswith("정지:") else None
            b.새업적 = []
            b.새로그 = []
            return _결과(True, 정보)
        elif action == "merchant":
            이름 = str(몸.get("name", ""))
            할인 = dict(b.상인).get(이름)
            if 할인 is None or b.상태 != "클리어":
                return _결과(False, "상인이 파는 물건이 아닙니다.")
            성공, r = RPG.구매(uid, 이름, 1, 할인=할인)
            b.기록(f"🛒 {이름} 구매 ({r['가격']}골드)" if 성공 else f"🛒 {r}")
        else:
            raise HTTPException(404, "없는 동작입니다.")

        정보 = _전투정보(b)
        b.새업적 = []
        b.새로그 = []
        return _결과(True, 정보)


# ============================================================
# TRPG (Cyberpunk 2020)
# ============================================================

import TRPG  # noqa: E402


@app.get("/trpg")
def TRPG화면():
    return FileResponse(os.path.join(STATIC_DIR, "trpg.html"), headers={"Cache-Control": "no-cache"})


async def _trpg요청(request):
    _쓰기요청_확인(request)
    uid = _로그인유저(request)["id"]
    _속도제한(uid, 0.35)
    몸 = await request.json() if (await request.body()) else {}
    return uid, 몸


def _트pg채널(uid):
    """웹 플레이는 유저마다 자기 방을 쓴다"""
    return f"web:{uid}"


def _trpg정보(uid):
    c = TRPG.캐릭터(uid)
    if c is None:
        return None
    상태, 스턴, 치명 = TRPG.부상정보(c["damage"])
    체격, btm = TRPG.체격정보(c["stat_body"])
    ch = _트pg채널(uid)

    무기이름 = c["weapon"] or "주먹"
    무기정보 = None
    if 무기이름 in TRPG.무기:
        분류, 스킬명, wa, 피해식, 연사, 사거리m, 가격, 설명 = TRPG.무기[무기이름]
        무기정보 = {"name": 무기이름, "damage": 피해식, "skill": 스킬명,
                    "wa": wa, "rof": 연사, "range": 사거리m, "desc": 설명}
    방어정보 = None
    if c["armor"] and c["armor"] in TRPG.방어구:
        sp, 부위들, 가격, 설명 = TRPG.방어구[c["armor"]]
        방어정보 = {"name": c["armor"], "sp": sp, "areas": 부위들}

    역할정보 = TRPG.역할표.get(c["role"], ("", "", "", []))
    사이버 = TRPG.설치목록(uid)
    보너스 = TRPG.사이버보너스(uid)

    cursor = TRPG.cursor
    cursor.execute("SELECT name FROM trpg_gear WHERE user_id = ? AND kind = 'program'", (str(uid),))
    프로그램들 = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT name, qty FROM trpg_gear WHERE user_id = ? AND kind = 'vehicle'", (str(uid),))
    차량들 = [{"name": n, "qty": q} for n, q in cursor.fetchall()]

    넷 = []
    for 이름 in TRPG.넷시스템목록(ch):
        d = TRPG.넷시스템(ch, 이름)
        if d:
            넷.append({"name": 이름, "difficulty": d["난이도"], "alarm": d["경보"],
                       "nodes": [{"n": n["번호"], "kind": n["종류"], "ice": n["ICE"], "open": n["뚫림"]}
                                 for n in d["노드"]]})

    return {
        "name": c["name"], "role": c["role"], "roleEmoji": 역할정보[0],
        "special": 역할정보[1], "specialDesc": 역할정보[2], "specialLv": c["special"],
        "stats": {k: c[f"stat_{k.lower()}"] for k in TRPG.능력치순서},
        "current": {k: TRPG.능력(c, k) for k in TRPG.능력치순서},
        "statNames": {k: v[0] for k, v in TRPG.능력치표.items()},
        "skills": c["스킬"],
        "damage": c["damage"], "wound": 상태, "stunPenalty": 스턴, "mortal": 치명,
        "stunSave": max(0, c["stat_body"] + 스턴),
        "deathSave": (c["stat_body"] - 치명) if 치명 is not None else None,
        "bodyType": 체격, "btm": btm,
        "humanity": TRPG.인간성(c), "humanityMax": TRPG.최대인간성(c),
        "money": c["money"], "linked": bool(c.get("linked")),
        "weapon": 무기정보, "armor": 방어정보,
        "cyber": [{"name": n, "qty": q, "desc": TRPG.사이버웨어.get(n, ("", "", 0, "", {}))[3]}
                  for n, q in 사이버.items()],
        "cyberBonus": {k: v for k, v in 보너스.items() if v},
        "martial": TRPG._내유파(uid),
        "programs": 프로그램들, "vehicles": 차량들, "net": 넷,
        "npcs": [{"name": n, "body": b, "ref": r, "skill": s, "armor": a,
                  "damage": d, "wound": TRPG.부상정보(d)[0]}
                 for n, b, r, s, a, d in TRPG.NPC목록(ch)],
        "initiative": [{"name": n, "roll": v} for n, v in TRPG.이니시목록(ch)],
        "lifepath": TRPG.라이프패스읽기(uid),
    }


@app.get("/api/trpg/me")
def TRPG내정보(request: Request):
    uid = _로그인유저(request)["id"]
    with _db락:
        정보 = _trpg정보(uid)
    if 정보 is None:
        rc = TRPG.RPG캐릭터읽기(uid)
        return {"ok": True, "result": None,
                "rpg": ({"name": rc["name"], "level": rc["level"]} if rc else None)}
    return {"ok": True, "result": 정보}


@app.get("/api/trpg/data")
def TRPG자료():
    """상점과 규칙 자료. 로그인 없이도 본다"""
    return {"ok": True, "result": {
        "roles": {n: {"emoji": d[0], "special": d[1], "desc": d[2], "skills": d[3]}
                  for n, d in TRPG.역할표.items()},
        "weapons": {n: {"class": d[0], "skill": d[1], "wa": d[2], "damage": d[3],
                        "rof": d[4], "range": d[5], "price": d[6], "desc": d[7]}
                    for n, d in TRPG.무기.items()},
        "armors": {n: {"sp": d[0], "areas": d[1], "price": d[2], "desc": d[3]}
                   for n, d in TRPG.방어구.items()},
        "items": {n: {"price": d[0], "desc": d[1]} for n, d in TRPG.아이템.items()},
        "cyber": {n: {"class": d[0], "loss": d[1], "price": d[2], "desc": d[3]}
                  for n, d in TRPG.사이버웨어.items()},
        "programs": {n: {"class": d[0], "str": d[1], "price": d[2], "desc": d[3]}
                     for n, d in TRPG.프로그램.items()},
        "ice": {n: {"class": d[0], "str": d[1], "damage": d[2], "desc": d[3]}
                for n, d in TRPG.ICE.items()},
        "vehicles": {n: {"class": d[0], "sdp": d[1], "sp": d[2], "speed": d[3],
                         "seats": d[4], "price": d[5], "desc": d[6]}
                     for n, d in TRPG.차량.items()},
        "martial": {n: {"diff": d[0], "bonus": d[1], "desc": d[2]} for n, d in TRPG.마셜아츠.items()},
        "techniques": {n: {"damage": d[0], "desc": d[1]} for n, d in TRPG.격투기술.items()},
        "difficulty": TRPG.난이도표,
        "ranges": {n: v[0] for n, v in TRPG.사거리표.items()},
        "driveSituations": TRPG.운전난이도,
        "stats": TRPG.능력치순서,
        "statNames": {k: {"ko": v[0], "desc": v[1]} for k, v in TRPG.능력치표.items()},
        "skills": {n: {"stat": d[0], "desc": d[1]} for n, d in TRPG.스킬표.items()},
        "guide": TRPG.설명,
        "guideOrder": list(TRPG.설명),
        "wounds": [{"upTo": u, "name": n, "stun": st, "mortal": m} for u, n, st, m in TRPG.부상표],
        "bodyTypes": [{"upTo": u, "name": n, "btm": b} for u, n, b in TRPG.체격표],
        "hitLocations": [{"range": list(r), "name": n} for r, n, _ in TRPG.부위표],
        "goldRate": TRPG.RPG_환율,
    }}


@app.post("/api/trpg/{action}")
async def TRPG조작(action: str, request: Request):
    uid, 몸 = await _trpg요청(request)
    ch = _트pg채널(uid)

    def 글(x):
        return str(몸.get(x) or "").strip()

    def 수(x, 기본=0):
        try:
            return int(몸.get(x, 기본))
        except (TypeError, ValueError):
            return 기본

    with _db락:
        # ---- 캐릭터 ----
        if action == "create":
            성공, r = TRPG.생성(uid, 글("name"), 글("role"))
            return _결과(성공, "ok" if 성공 else r)

        if action == "createLinked":
            성공, r = TRPG.RPG연동생성(uid, 글("role"), 글("name") or None)
            return _결과(성공, r)

        if action == "delete":
            성공, r = TRPG.삭제(uid)
            return _결과(성공, r)

        if action == "stats":
            값 = 몸.get("stats") or {}
            ok, r = TRPG.능력치_검사(값)
            if not ok:
                return _결과(False, r)
            TRPG.cursor.execute(
                "UPDATE trpg_characters SET stat_int=?, stat_ref=?, stat_tech=?, stat_cool=?, "
                "stat_attr=?, stat_luck=?, stat_ma=?, stat_body=?, stat_emp=?, emp_base=? WHERE user_id=?",
                (r["INT"], r["REF"], r["TECH"], r["COOL"], r["ATTR"], r["LUCK"],
                 r["MA"], r["BODY"], r["EMP"], r["EMP"], str(uid)))
            TRPG.conn.commit()
            return _결과(True, "ok")

        if action == "skill":
            성공, r = TRPG.스킬설정(uid, 글("name"), 수("level"))
            return _결과(성공, r)

        if action == "lifepath":
            d = TRPG.라이프패스생성()
            TRPG.라이프패스저장(uid, d)
            return _결과(True, d)

        # ---- 판정 ----
        if action == "roll":
            r, err = TRPG.판정(uid, 글("stat"), 글("skill") or None,
                              몸.get("difficulty"), 수("modifier"))
            return _결과(err is None, r if err is None else err)

        if action == "attack":
            r, err = TRPG.공격(ch, 공격자_uid=uid, 대상=글("target"),
                              무기이름=글("weapon") or None, 사거리=글("range") or "중거리",
                              보정=수("modifier"), 연사=수("burst", 1))
            if err:
                return _결과(False, err)
            return _결과(True, {"data": r, "text": TRPG.공격문장(r)})

        if action == "damage":
            대상 = 글("target") or "me"
            isnpc = 대상.lower() != "me" and TRPG.NPC(ch, 대상) is not None
            r, err = TRPG.피해적용(ch, 대상 if isnpc else uid, 수("amount"), NPC여부=isnpc)
            return _결과(err is None, r if err is None else err)

        if action == "heal":
            성공, r = TRPG.회복(uid, 몸.get("amount"))
            return _결과(성공, r)

        if action == "stabilize":
            대상 = 글("target") or "me"
            isnpc = 대상.lower() != "me" and TRPG.NPC(ch, 대상) is not None
            r, err = TRPG.안정화(uid, 대상 if isnpc else uid, NPC여부=isnpc, channel_id=ch)
            return _결과(err is None, r if err is None else err)

        # ---- 장비 ----
        if action == "buy":
            성공, r = TRPG.구매(uid, 글("name"), 수("qty", 1))
            return _결과(성공, r)

        if action == "equip":
            성공, r = TRPG.장비장착(uid, 글("name"))
            return _결과(성공, r)

        # ---- 사이버웨어 ----
        if action == "cyberInstall":
            성공, r = TRPG.사이버설치(uid, 글("name"))
            return _결과(성공, r)

        if action == "cyberRemove":
            성공, r = TRPG.사이버제거(uid, 글("name"))
            return _결과(성공, r)

        if action == "sanity":
            r, err = TRPG.사이버사이코판정(uid)
            return _결과(err is None, r if err is None else err)

        # ---- 넷러닝 ----
        if action == "programBuy":
            성공, r = TRPG.프로그램구매(uid, 글("name"), 수("qty", 1))
            return _결과(성공, r)

        if action == "netCreate":
            d = TRPG.넷시스템생성(ch, 글("name") or "무명 시스템", 글("difficulty") or "보통")
            return _결과(True, d)

        if action == "netRun":
            r, err = TRPG.넷침입(uid, ch, 글("system"), 글("program") or None, 수("modifier"))
            if err:
                return _결과(False, err)
            return _결과(True, {"data": r, "text": TRPG.넷침입글(r)})

        if action == "netClear":
            return _결과(True, TRPG.넷시스템삭제(ch, 글("name") or None))

        # ---- 차량 ----
        if action == "vehicleBuy":
            성공, r = TRPG.차량구매(uid, 글("name"), 수("qty", 1))
            return _결과(성공, r)

        if action == "drive":
            r, err = TRPG.운전판정(uid, 글("situation") or "추격전",
                                 수("modifier"), 글("vehicle") or None)
            return _결과(err is None, r if err is None else err)

        # ---- 격투 ----
        if action == "learnMartial":
            성공, r = TRPG.유파배우기(uid, 글("name"))
            return _결과(성공, r)

        if action == "melee":
            r, err = TRPG.격투판정(uid, 글("technique") or "타격",
                                 대상=글("target") or None, channel_id=ch, 보정=수("modifier"))
            if err:
                return _결과(False, err)
            return _결과(True, {"data": r, "text": TRPG.격투글(r)})

        # ---- NPC / 이니셔티브 ----
        if action == "npcAdd":
            r = TRPG.NPC추가(ch, 글("name") or "적", 수("body", 5), 수("ref", 5),
                            수("skill", 4), 수("armor", 0), 글("weapon") or "중권총")
            return _결과(True, r)

        if action == "npcClear":
            return _결과(True, TRPG.NPC정리(ch))

        if action == "initiative":
            if 글("npc"):
                n = TRPG.NPC(ch, 글("npc"))
                if n is None:
                    return _결과(False, "그런 NPC가 없습니다.")
                r, err = TRPG.이니시굴림(ch, 이름=n["name"], REF=n["ref"])
            else:
                r, err = TRPG.이니시굴림(ch, user_id=uid)
            return _결과(err is None, r if err is None else err)

        if action == "initiativeClear":
            return _결과(True, TRPG.이니시초기화(ch))

        # ---- 환전 ----
        if action == "exchange":
            성공, r = TRPG.골드환전(uid, 글("dir") or "골드→eb", 수("qty"))
            return _결과(성공, r)

    raise HTTPException(404, "없는 동작입니다.")
