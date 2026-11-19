"""
주식 — 주가 엔진·ETF·캔들 차트·레버리지·배당/예약 주문.
봇·웹·AI 툴(tools.py)이 같이 쓴다. 고치면 봇 재시작 + pm2 restart web.

  1. 주가 엔진   실제 코스피·코스닥 통계(시장자료.json)로 base_price 를 움직인다
  2. ETF         지수·업종 ETF, NAV 매일 리셋
  3. 캔들 차트   !주식 그래프 PNG
  4. 레버리지    2·4·8·16배 롱/인버스, 강제 청산·자동 충전
  5. 배당 + 예약 주문 (지정가·손절)
"""
import hashlib
import io
import json
import math
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

import httpx
from PIL import Image, ImageDraw, ImageFont


# ============================================================
# 1. 주가 엔진 — 장 시간·캔들·VI·서킷·실적·거래량
# ============================================================
# 주식 시장 엔진 (코스피·코스닥). 코인은 업비트 실시간 시세라 여기서 다루지 않는다.
#
# 실제 한국 주식 3년치로 만든 시장자료.json (시장자료_수집.py 가 만든다) 을 바탕으로 주가를 움직인다.
#   - 시장 전체 : 실제 코스피·코스닥 지수의 '하루'를 통째로 뽑아 쓴다 (갭, 장중 등락, 장중 변동폭).
#                며칠씩 이어서 뽑기 때문에 오르는 장·내리는 장·출렁이는 장이 실제처럼 이어진다.
#   - 업종      : 종목마다 업종(stocks.sector)이 있고, 업종마다 실제 그 업종 종목들이 지수와 따로 움직인 만큼을
#                지수와 같은 날짜에서 뽑아 쓴다. 그래서 "반도체가 끌고 가는 날", "바이오만 빠지는 날"이 실제처럼 나온다.
#   - 종목 고유 : 봇 종목마다 같은 업종 실제 종목 하나의 성격(베타, 변동성, 업종 민감도)을 물려받고,
#                실제 종목들의 하루 움직임 표본(급등·급락 포함)에서 그날의 등락을 뽑는다.
#                어제 크게 움직였으면 오늘도 변동성이 크다 (GARCH).
#   - 장중 흐름 : 시가(갭)에서 출발해 그날의 종가 목표로 흘러간다 (브라운 브리지).
#                장 초반·막판에 변동이 큰 실제 U자 모양을 따른다.
#   - 가격제한폭 ±50%, 큰 움직임은 장중 한 시점에 '뉴스'로 터진다.
#
# 주가 = 기준가(base_price, 이 엔진이 움직임) × 수급(보유 비율, tools.가격계산).
# 기준가는 소수까지 적어 둔다 (정수로 자르면 싼 종목이 5초 단위로는 못 움직인다).
#
# 봇만 틱()을 돌린다. 웹은 캔들·장 상태만 읽는다.

여기 = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(여기, "bot.db")
자료파일 = os.path.join(여기, "시장자료.json")

# ---------- 설정 ----------
개장 = (7, 30)             # 장 여는 시각 (시, 분)
폐장 = (20, 0)             # 장 닫는 시각. 개장보다 늦어야 한다 (자정을 넘기지 않는다)
휴장요일 = ()              # 쉬는 요일. 0=월 … 6=일. 예: (5, 6) 이면 주말 휴장
틱_초 = 5                  # 주가를 움직이는 간격 (봇의 주가변동 루프가 이 값을 쓴다)
변동성_배율 = 6.0          # 1.0 = 실제 한국 주식 그대로. 2.0 이면 등락 폭이 두 배 (2026-11-18 3.0 → 6.0)
갭_배율 = 1.5              # 개장 갭(전일 종가 → 시가 점프)에만 쓰는 배율. 장중 움직임은 변동성_배율 그대로
가격제한폭 = 0.50          # 전일 종가 대비 ±50% (상한가·하한가). 변동성_배율을 올리면 같이 넓힌다
뉴스_기준 = 0.36           # (변동성_배율에 비례) 그날 종목 고유 등락이 이보다 크면 장중에 뉴스로 한 번에 터진다
분봉_보관일 = 8            # 1분봉 보관 기간. 일봉은 계속 둔다
장기추세_제거 = True       # 자료 기간(3년) 동안 지수가 오른 만큼을 뺀다. 며칠~몇 주짜리 상승장·하락장은 그대로 남는다.
                           # False 면 그 3년의 상승세(코스피 하루 평균 +0.11%)가 게임에도 계속 이어진다

# 거래 정지 — 실제 한국 시장 규칙을 본뜬다
VI_기준 = 0.20             # 정적 VI (변동성_배율에 비례, 실제는 10%): 기준가(그날 시가, 또는 직전 VI 가 풀린 가격)에서 ±20% 움직이면 그 종목만 멈춘다
VI_분 = 2                  # VI 로 멈추는 시간 (분)
서킷_단계 = ((0.16, 20), (0.30, 20), (0.40, None))   # (변동성_배율에 비례, 실제는 8·15·20%) 서킷브레이커: 지수가 전일 대비 이만큼 빠지면 그 시장 전체를 (분) 멈춘다. None = 그날 장 끝
서킷_막판분 = 40           # 마감 이 분 전부터는 1·2단계 서킷브레이커를 걸지 않는다 (실제 규칙)

# 실적 발표 — 분기마다 종목별로 정해진 날 장중에 발표하고, 그날은 크게 움직인다
실적_시즌 = ((1, 20, 2, 14), (4, 15, 5, 15), (7, 15, 8, 14), (10, 15, 11, 14))   # (시작 월, 일, 끝 월, 일)
실적_변동배율 = 2.5        # 발표날 종목 고유 등락 크기 배율

# 거래량 — 하루 거래량 ≈ 유통량 × 회전율. 많이 움직이는 날·뉴스·VI 해제 때 몰린다. 유저 매매도 더한다
회전율 = {1: 0.15, 0: 0.30}

_ETF아님 = "symbol NOT IN (SELECT symbol FROM stock_etfs WHERE removed = 0)"
기본업종 = "종합"          # 업종을 안 정했고 이름으로도 짐작이 안 되는 종목
업종_최소민감도 = 0.5      # 업종이 움직일 때 종목이 따라가는 정도의 최소값 (실제 평균은 0.8쯤)

_잡음_최소 = 0.4           # 장중 흔들림의 최소 크기 (그날 변동성 대비)
_업종풀_최소 = 5           # 같은 시장·업종의 실제 종목이 이보다 적으면 시장을 안 가리고 고른다
_지수잡음_최소 = 0.004
_요일 = "월화수목금토일"

# 이름이 같은 실제 종목이 있으면 그 종목의 성격을 쓴다. 나머지는 종목 코드로 정해진다.
실제종목 = {"SAMSUNG": "005930", "SK": "000660", "NAVER": "035420", "KAKAO": "035720", "HYUNDAI": "005380"}


# ============================================================
# 테이블
# ============================================================
def _준비():
    c = sqlite3.connect(DB, timeout=15)
    c.execute(
        "CREATE TABLE IF NOT EXISTS stock_candles ("
        "symbol TEXT NOT NULL, tf TEXT NOT NULL, ts TEXT NOT NULL, "
        "o INTEGER, h INTEGER, l INTEGER, c INTEGER, "
        "PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID"
    )
    c.execute("CREATE TABLE IF NOT EXISTS stock_sim (symbol TEXT PRIMARY KEY, data TEXT)")
    # 배당 기록 (배당 칸이 기준일 마감 뒤 적고, 엔진은 다음 개장 때 배당락을 반영한 뒤 ex_done=1)
    c.execute(
        "CREATE TABLE IF NOT EXISTS stock_dividends ("
        "date TEXT NOT NULL, symbol TEXT NOT NULL, dps INTEGER, rate REAL, "
        "holders INTEGER DEFAULT 0, total INTEGER DEFAULT 0, ex_done INTEGER DEFAULT 0, "
        "PRIMARY KEY (date, symbol))"
    )
    # ETF (ETF 칸). 상장폐지하면 removed=1 로 남겨서 다시 만들지 않는다
    c.execute(
        "CREATE TABLE IF NOT EXISTS stock_etfs ("
        "symbol TEXT PRIMARY KEY, lev REAL NOT NULL, rule TEXT NOT NULL, fee REAL DEFAULT 0, "
        "descr TEXT, state TEXT, created TEXT, removed INTEGER DEFAULT 0)"
    )
    for 문 in ("ALTER TABLE stocks ADD COLUMN sector TEXT",            # 업종. 비어 있으면 이름으로 짐작한다
              "ALTER TABLE stock_candles ADD COLUMN v REAL DEFAULT 0"):  # 거래량 (주)
        try:
            c.execute(문)
        except sqlite3.OperationalError:
            pass                                                    # 이미 있거나, 봇이 아직 stocks 를 안 만들었다
    c.commit()
    c.close()


_준비()


# ============================================================
# 장 시간
# ============================================================
def 장시간(날짜):
    """그날의 (개장, 폐장) 시각"""
    return (datetime(날짜.year, 날짜.month, 날짜.day, *개장),
            datetime(날짜.year, 날짜.month, 날짜.day, *폐장))


def 장중(지금=None):
    지금 = 지금 or datetime.now()
    if 지금.weekday() in 휴장요일:
        return False
    열, 닫 = 장시간(지금)
    return 열 <= 지금 < 닫


def 다음개장(지금=None):
    지금 = 지금 or datetime.now()
    날 = 지금
    for _ in range(9):
        열, _닫 = 장시간(날)
        if 날.weekday() not in 휴장요일 and 열 > 지금:
            return 열
        날 += timedelta(days=1)
    return None


def 시간표():
    """예: '매일 09:00~23:00', '월~금 09:00~15:30'"""
    여는날 = [i for i in range(7) if i not in 휴장요일]
    if len(여는날) == 7:
        요일 = "매일"
    elif 여는날 == list(range(여는날[0], 여는날[-1] + 1)):
        요일 = f"{_요일[여는날[0]]}~{_요일[여는날[-1]]}"
    else:
        요일 = "·".join(_요일[i] for i in 여는날)
    return f"{요일} {개장[0]:02d}:{개장[1]:02d}~{폐장[0]:02d}:{폐장[1]:02d}"


def _언제(시각, 지금):
    차 = (시각.date() - 지금.date()).days
    날 = "오늘" if 차 == 0 else "내일" if 차 == 1 else f"{_요일[시각.weekday()]}요일"
    return f"{날} {시각:%H:%M}"


def 장상태(지금=None):
    """{"open", "text", "hours", "until"} — until 은 장중이면 폐장, 아니면 다음 개장 시각(ISO)"""
    지금 = 지금 or datetime.now()
    if 장중(지금):
        닫 = 장시간(지금)[1]
        return {"open": True, "text": f"장중 · {닫:%H:%M} 마감", "hours": 시간표(),
                "until": 닫.isoformat(timespec="seconds")}
    열 = 다음개장(지금)
    return {"open": False, "text": f"장 마감 · {_언제(열, 지금)} 개장" if 열 else "장 마감",
            "hours": 시간표(), "until": 열.isoformat(timespec="seconds") if 열 else None}


def 거래불가(지금=None):
    """장중이면 None, 아니면 유저에게 보여줄 안내 문구"""
    지금 = 지금 or datetime.now()
    if 장중(지금):
        return None
    열 = 다음개장(지금)
    return (f"지금은 장이 닫혀 있어 주식을 사고팔 수 없습니다. "
            f"(장 시간: {시간표()}" + (f", 다음 개장 {_언제(열, 지금)}" if 열 else "") + ") "
            f"코인은 24시간 거래됩니다.")


# ============================================================
# 캔들 (웹 차트·등락률)
# ============================================================
_캔들문 = (
    "INSERT INTO stock_candles (symbol, tf, ts, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(symbol, tf, ts) DO UPDATE SET "
    "h = MAX(h, excluded.h), l = MIN(l, excluded.l), c = excluded.c, v = COALESCE(v, 0) + excluded.v"
)


def 캔들기록(cursor, 심볼, 가격, 지금=None, 거래량=0.0):
    """지금 가격을 1분봉·일봉에 반영하고 거래량(주)을 더한다. 커밋은 부른 쪽에서."""
    지금 = 지금 or datetime.now()
    p = int(가격)
    v = float(거래량 or 0)
    cursor.execute(_캔들문, (심볼, "1m", 지금.strftime("%Y-%m-%dT%H:%M"), p, p, p, p, v))
    cursor.execute(_캔들문, (심볼, "1d", 지금.strftime("%Y-%m-%d"), p, p, p, p, v))


def 오늘거래량(cursor, 지금=None):
    """{심볼: (거래량 주, 거래대금 원)} — 오늘 일봉 기준. 장이 닫혀 있으면 마지막 거래일"""
    cursor.execute("SELECT MAX(ts) FROM stock_candles WHERE tf = '1d'")
    날 = cursor.fetchone()[0]
    if not 날:
        return {}
    cursor.execute("SELECT symbol, COALESCE(v, 0), o, c FROM stock_candles WHERE tf = '1d' AND ts = ?", (날,))
    return {s: (v, int(v * (o + c) / 2)) for s, v, o, c in cursor.fetchall()}


def 캔들정리(cursor):
    기준 = (datetime.now() - timedelta(days=분봉_보관일)).strftime("%Y-%m-%dT%H:%M")
    cursor.execute("DELETE FROM stock_candles WHERE tf = '1m' AND ts < ?", (기준,))


def 전일종가들(cursor):
    """{심볼: 등락률 기준가}. 마지막 거래일의 바로 앞 거래일 종가. 첫날이면 그날 시가."""
    cursor.execute(
        "SELECT symbol, o, c, n FROM ("
        "  SELECT symbol, o, c, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS n"
        "  FROM stock_candles WHERE tf = '1d') WHERE n <= 2 ORDER BY symbol, n"
    )
    결과 = {}
    for 심볼, o, c, n in cursor.fetchall():
        결과[심볼] = o if n == 1 else c      # n=2 (전 거래일) 가 있으면 그 종가로 덮어쓴다
    return 결과


# 구간 → (봉 단위, 묶는 분, 범위 분). 범위는 마지막 봉에서 거꾸로 센다 (장이 닫혀 있어도 비지 않게).
_구간 = {"1h": (1, 60), "1d": (5, 24 * 60), "1w": (60, 7 * 24 * 60)}


def 캔들조회(cursor, 심볼, 구간="1d"):
    """[(시각 ISO, 시, 고, 저, 종, 거래량)] 오래된 것부터. 구간: 1h(1분봉) 1d(5분봉) 1w(1시간봉) all(일봉)"""
    if 구간 == "all":
        cursor.execute(
            "SELECT ts, o, h, l, c, COALESCE(v, 0) FROM stock_candles WHERE symbol = ? AND tf = '1d' "
            "ORDER BY ts DESC LIMIT 1100", (심볼,))
        return [(t + "T00:00:00", o, h, l, c, v) for t, o, h, l, c, v in reversed(cursor.fetchall())]

    묶음, 범위 = _구간.get(구간, _구간["1d"])
    cursor.execute("SELECT MAX(ts) FROM stock_candles WHERE symbol = ? AND tf = '1m'", (심볼,))
    마지막 = cursor.fetchone()[0]
    if not 마지막:
        return []
    if 구간 == "1h":
        cursor.execute(
            "SELECT ts, o, h, l, c, COALESCE(v, 0) FROM stock_candles WHERE symbol = ? AND tf = '1m' "
            "ORDER BY ts DESC LIMIT 60", (심볼,))
        return [(t + ":00", o, h, l, c, v) for t, o, h, l, c, v in reversed(cursor.fetchall())]

    시작 = (datetime.fromisoformat(마지막) - timedelta(minutes=범위)).strftime("%Y-%m-%dT%H:%M")
    cursor.execute(
        "SELECT ts, o, h, l, c, COALESCE(v, 0) FROM stock_candles WHERE symbol = ? AND tf = '1m' AND ts > ? ORDER BY ts",
        (심볼, 시작))
    봉들 = []
    for t, o, h, l, c, v in cursor.fetchall():
        분 = int(t[11:13]) * 60 + int(t[14:16])
        분 -= 분 % 묶음
        키 = f"{t[:11]}{분 // 60:02d}:{분 % 60:02d}:00"
        if 봉들 and 봉들[-1][0] == 키:
            b = 봉들[-1]
            b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
        else:
            봉들.append([키, o, h, l, c, v])
    return [tuple(b) for b in 봉들]


def 마지막캔들(cursor, 심볼, 구간="1d"):
    """캔들조회의 마지막 봉 하나만 (웹이 몇 초마다 부른다). 없으면 None"""
    if 구간 == "all":
        cursor.execute(
            "SELECT ts, o, h, l, c, COALESCE(v, 0) FROM stock_candles WHERE symbol = ? AND tf = '1d' "
            "ORDER BY ts DESC LIMIT 1", (심볼,))
        r = cursor.fetchone()
        return (r[0] + "T00:00:00",) + tuple(r[1:]) if r else None
    cursor.execute("SELECT MAX(ts) FROM stock_candles WHERE symbol = ? AND tf = '1m'", (심볼,))
    마지막 = cursor.fetchone()[0]
    if not 마지막:
        return None
    묶음 = 1 if 구간 == "1h" else _구간.get(구간, _구간["1d"])[0]
    분 = int(마지막[11:13]) * 60 + int(마지막[14:16])
    분 -= 분 % 묶음
    시작 = f"{마지막[:11]}{분 // 60:02d}:{분 % 60:02d}"
    cursor.execute(
        "SELECT o, h, l, c, COALESCE(v, 0) FROM stock_candles WHERE symbol = ? AND tf = '1m' AND ts >= ? ORDER BY ts",
        (심볼, 시작))
    봉 = cursor.fetchall()
    return (시작 + ":00", 봉[0][0], max(b[1] for b in 봉), min(b[2] for b in 봉), 봉[-1][3], sum(b[4] for b in 봉))


# ============================================================
# 실제 시장 자료
# ============================================================
_자료 = None
_기본모양 = [0.17, 0.07, 0.08, 0.05, 0.055, 0.055, 0.04, 0.037, 0.031, 0.029, 0.027, 0.021, 0.021,
            0.021, 0.019, 0.02, 0.022, 0.021, 0.027, 0.024, 0.025, 0.029, 0.03, 0.024, 0.026, 0.017]


def 자료():
    global _자료
    if _자료 is None:
        with open(자료파일, encoding="utf-8") as f:
            d = json.load(f)
        모양 = d.get("장중모양") or _기본모양
        합 = sum(모양)
        d["모양"] = [w / 합 for w in 모양]
        누적, 쌓임 = [], 0.0
        for w in d["모양"]:
            누적.append(쌓임)
            쌓임 += w
        d["누적"] = 누적
        d["찾기"] = {s[0]: s for 풀 in d["종목"].values() for s in 풀}
        n = len(d["지수"])
        d["추세"] = {"1": sum(r[1] + r[2] for r in d["지수"]) / n, "0": sum(r[4] + r[5] for r in d["지수"]) / n}
        d["평균폭"] = sum(r[3] + r[6] for r in d["지수"]) / n / 2
        d["업종"] = d.get("업종") or {}
        d["업종날"] = d.get("업종날") or {}
        풀들 = {}                                   # (시장, 업종) 또는 업종 → 실제 종목들 (시가총액 순)
        for k, 풀 in d["종목"].items():
            for s in 풀:
                if len(s) > 5 and s[4] in d["업종"]:
                    풀들.setdefault((k, s[4]), []).append(s)
                    풀들.setdefault(s[4], []).append(s)
        d["업종풀"] = 풀들
        _자료 = d
    return _자료


def _타우(비율):
    """장 진행률(0~1, 시계 기준) → 변동 진행률(0~1). 장 초반·막판에 빨리 흐른다."""
    d = 자료()
    if 비율 <= 0:
        return 0.0
    if 비율 >= 1:
        return 1.0
    x = 비율 * len(d["모양"])
    k = int(x)
    return d["누적"][k] + d["모양"][k] * (x - k)


def 유전자(심볼, 시장, 업종=None):
    """이 종목이 물려받은 실제 종목 [코드, 이름, 베타, 고유 변동성, 업종, 업종 민감도]. 같은 업종에서 고른다."""
    d = 자료()
    코드 = 실제종목.get(심볼)
    if 코드 in d["찾기"]:
        return d["찾기"][코드]
    풀 = d["업종풀"].get((str(시장), 업종)) or []
    if len(풀) < _업종풀_최소:
        풀 = d["업종풀"].get(업종) or d["종목"][str(시장)]
    return 풀[int(hashlib.md5(심볼.encode()).hexdigest(), 16) % len(풀)]


def _성격(심볼, 시장, 업종):
    """(물려받은 실제 종목 코드, 베타, 고유 변동성, 업종 민감도)"""
    s = 유전자(심볼, 시장, 업종)
    정보 = 자료()["업종"].get(업종)
    if not 정보:
        감마 = 0.0
    else:       # 다른 업종의 실제 종목을 물려받았으면 (이름이 같은 실제 종목 등) 그 업종의 평균 민감도를 쓴다
        감마 = max(업종_최소민감도, s[5] if len(s) > 5 and s[4] == 업종 else 정보.get("감마", 0.0))
    return s[0], s[2], s[3], 감마


# ============================================================
# 업종
# ============================================================
# 이름에 이 낱말이 있으면 그 업종으로 본다. 업종을 안 정하고 상장한 종목에만 쓴다. 위에서부터 먼저 맞는 것.
_업종단어 = (
    ("반도체", ("반도체", "하이닉스", "메모리")),
    ("배터리", ("배터리", "전지")),
    ("게임", ("게임", "닌텐도", "크래프트", "피파", "타워", "라이벌", "갓필드")),
    ("엔터", ("유튜브", "방송", "채널", "디즈니", "엔터", "먹방", "뮤직", "음악", "폰크", "영화")),
    ("IT", ("소프트", "네이버", "카카오", "구글", "알파벳", "봇", ".py", "인터넷", "링고", "링그", "클라우드")),
    ("전자", ("전자", "애플", "노키아", "가전", "컴퓨터")),
    ("자동차", ("자동차", "현대", "기아", "테슬라", "모터스")),
    ("바이오", ("바이오", "제약", "약품", "병원", "의료")),
    ("화학", ("화학", "정유", "석유", "에너지")),
    ("광업", ("광업", "광산", "철강", "제철", "금속", "광물", "금광")),
    ("조선", ("조선", "보물선", "선박")),
    ("중공업", ("중공업", "산업", "기계", "로봇", "방산", "우주", "로켓")),
    ("건설", ("건설", "건축", "부동산", "시멘트", "아파트")),
    ("금융", ("금융", "은행", "증권", "보험", "카드", "투자", "캐피탈", "뱅크")),
    ("식품", ("식품", "식당", "치킨", "비비큐", "kfc", "돼지", "고기", "튀김", "푸드", "음료", "카페", "커피",
            "버거", "피자", "豚", "ぶた", "🐷", "𓃟")),
    ("유통", ("유통", "쇼핑", "마트", "백화점", "테무", "아마존", "아마돈", "롯데", "쿠팡", "무역")),
    ("수공업", ("수공업", "공방", "가구", "이케아", "의류", "화장품", "부적", "공예", "수제")),
    ("운송", ("운송", "항공", "해운", "택배", "물류", "철도")),
    ("공공", ("전력", "가스", "통신", "텔레콤")),
)

# 유저가 업종 이름 대신 칠 만한 말
_업종별칭 = {
    "디스플레이": "반도체", "가전": "전자", "2차전지": "배터리", "전지": "배터리", "인터넷": "IT", "소프트웨어": "IT",
    "아이티": "IT", "엔터테인먼트": "엔터", "방송": "엔터", "연예": "엔터", "제약": "바이오", "의료": "바이오",
    "에너지": "화학", "정유": "화학", "철강": "광업", "금속": "광업", "기계": "중공업", "방산": "중공업",
    "부동산": "건설", "은행": "금융", "증권": "금융", "보험": "금융", "외식": "식품", "음식": "식품",
    "쇼핑": "유통", "무역": "유통", "경공업": "수공업", "의류": "수공업", "화장품": "수공업", "가구": "수공업",
    "해운": "운송", "항공": "운송", "물류": "운송", "전력": "공공", "통신": "공공", "가스": "공공",
    "지주": "종합", "서비스": "종합", "교육": "종합", "기타": "종합",
}


def 업종목록():
    """{업종: 설명} — 시장자료.json 에 있는 순서대로"""
    return {이름: 정보.get("설명", "") for 이름, 정보 in 자료()["업종"].items()}


def 업종찾기(글):
    """유저가 친 글 → 업종 이름. 없는 업종이면 None"""
    글 = (글 or "").strip().lower().replace(" ", "")
    업종들 = 자료()["업종"]
    for 이름 in 업종들:
        if 이름.lower() == 글:
            return 이름
    별칭 = _업종별칭.get(글)
    return 별칭 if 별칭 in 업종들 else None


def 종목업종(값, 이름=""):
    """stocks.sector 값 → 실제로 쓰는 업종. 비었거나 없는 업종이면 종목 이름으로 짐작한다."""
    업종들 = 자료()["업종"]
    if 값 in 업종들:
        return 값
    if not 업종들:
        return None
    소문자 = (이름 or "").lower()
    for 업, 낱말들 in _업종단어:
        if 업 in 업종들 and any(w in 소문자 for w in 낱말들):
            return 업
    return 기본업종 if 기본업종 in 업종들 else next(iter(업종들))


def 업종정보(업종):
    """업종 하나의 실제 자료 요약. 없는 업종이면 None"""
    d = 자료()
    정보 = d["업종"].get(업종)
    if not 정보:
        return None
    return {"name": 업종, "desc": 정보.get("설명", ""), "vol": 정보.get("변동", 0.0), "share": 정보.get("몫", 0.0),
            "real": [s[1] for s in (d["업종풀"].get(업종) or [])[:40]]}


def 업종현황(cursor):
    """
    업종별 오늘 등락 (그 업종 종목들의 전일 대비 등락률 평균).
    [{name, desc, change(종목이 없으면 None), stocks: [{symbol, name, market, price, change}]}]
    종목이 있는 업종을 많이 오른 순서로, 그 뒤에 빈 업종.
    """
    전일 = 전일종가들(cursor)
    cursor.execute("SELECT symbol, name, is_premium, price, sector FROM stocks "
                   f"WHERE is_premium IN (0, 1) AND {_ETF아님} ORDER BY is_premium DESC, symbol")
    묶음 = {이름: [] for 이름 in 자료()["업종"]}
    for 심볼, 이름, 시장, 가격, 업종 in cursor.fetchall():
        업 = 종목업종(업종, 이름)
        if 업 is None:
            continue
        가격 = int(float(가격))
        기준 = 전일.get(심볼)
        묶음[업].append({"symbol": 심볼, "name": 이름, "market": 시장, "price": 가격,
                        "change": (가격 - 기준) / 기준 if 기준 else 0.0})
    결과 = []
    for 업, 종목들 in 묶음.items():
        종목들.sort(key=lambda s: -s["change"])
        결과.append({"name": 업, "desc": 자료()["업종"][업].get("설명", ""), "stocks": 종목들,
                    "change": sum(s["change"] for s in 종목들) / len(종목들) if 종목들 else None})
    결과.sort(key=lambda u: (u["change"] is None, -(u["change"] or 0)))
    return 결과


def 업종설정(cursor, 심볼, 업종):
    """종목의 업종을 바꾼다. 장중이면 다음 틱부터 새 업종을 따라간다. 커밋은 부른 쪽에서."""
    cursor.execute(f"UPDATE stocks SET sector = ? WHERE symbol = ? AND is_premium IN (0, 1) AND {_ETF아님}", (업종, 심볼))
    return cursor.rowcount > 0


# ============================================================
# 엔진 상태
#   _장    : 오늘 시장 전체의 상태 (stock_sim 의 '*' 행). 매 틱 저장한다.
#   _종목  : 종목별 오늘의 계획 (시가, 종가 목표, 뉴스 …). 바뀔 때만 저장한다.
#   지금 어디까지 왔는지는 따로 적지 않고 DB의 기준가에서 거꾸로 계산한다.
#   그래서 봇을 재시작하거나 관리자가 !시세조정을 해도 그대로 이어진다.
# ============================================================
_장 = None
_종목 = {}
_쓴값 = {}        # 심볼 → 엔진이 마지막으로 쓴 기준가 글자 (밖에서 바꿨는지 알아내려고)
_쓴분 = {}        # 심볼 → 마지막으로 캔들을 쓴 분
_불러옴 = False


def _불러오기(cursor):
    global _장, _불러옴
    cursor.execute("SELECT symbol, data FROM stock_sim")
    for 심볼, 글 in cursor.fetchall():
        try:
            값 = json.loads(글)
        except ValueError:
            continue
        if 심볼 == "*":
            _장 = 값
        else:
            _종목[심볼] = 값
    _불러옴 = True


def _저장(cursor, 심볼, 값):
    cursor.execute("INSERT OR REPLACE INTO stock_sim (symbol, data) VALUES (?, ?)",
                   (심볼, json.dumps(값, ensure_ascii=False)))


def _종목행(cursor):
    """[{sym, name, 시장, 업종, price, base(소수), 글, supply, held}] — 기준가·유통량이 빈 종목은 tools 가 채우게 한다"""
    import tools
    cursor.execute(
        "SELECT s.symbol, s.name, s.is_premium, s.price, s.base_price, s.supply, "
        "COALESCE((SELECT SUM(CAST(p.quantity AS INTEGER)) FROM portfolio p WHERE p.symbol = s.symbol), 0), "
        "s.sector FROM stocks s WHERE s.is_premium IN (0, 1) "
        "AND s.symbol NOT IN (SELECT symbol FROM stock_etfs WHERE removed = 0)"      # ETF 는 진행() 이 따로
    )
    행들 = []
    for 심볼, 이름, 시장, 가격, 기준, 유통, 보유, 업종 in cursor.fetchall():
        try:
            기준값, 유통값 = float(기준 or 0), int(float(유통 or 0))
        except ValueError:
            기준값, 유통값 = 0.0, 0
        if 기준값 < 1 or 유통값 < 1:
            tools.시장정보(심볼)          # 빈 값을 채워 준다. 이 종목은 다음 틱부터 움직인다
            continue
        행들.append({"sym": 심볼, "name": 이름, "시장": 시장, "업종": 종목업종(업종, 이름), "price": int(float(가격)),
                    "base": 기준값, "글": 기준, "supply": 유통값, "held": int(보유 or 0)})
    return 행들


def _하루끝(st, L, 설명됨):
    """그날의 고유 등락(지수·업종 몫을 뺀 것)으로 내일 변동성을 정한다 (GARCH). 한 번만 한다."""
    if st.get("끝"):
        return
    a, b = 자료()["garch"][str(st["시장"])]
    e = (L - st["pc"] - 설명됨) / max(변동성_배율, 1e-9)
    긴 = st["v"] ** 2
    s2 = 긴 * (1 - a - b) + a * min(e * e, 0.09) + b * st["s2"]
    st["s2"] = max(긴 * 0.25, min(긴 * 16, s2))
    st["끝"] = True


def _뉴스계획(st, 시작):
    """그날 고유 등락이 크면, 그 대부분이 장중 한 시점에 터지게 한다"""
    st["뉴스"] = None
    if abs(st["tgt"]) >= 뉴스_기준 and 시작 < 0.8:
        st["뉴스"] = {"tau": random.uniform(max(0.03, 시작 + 0.02), 0.88),
                     "J": st["tgt"] * random.uniform(0.6, 0.95)}


def _새하루(행, 날짜, 장, 처음, 위치=None):
    """
    종목의 오늘 계획을 세운다.
      처음=True  : 개장. 전일 종가에서 갭을 두고 시작한다.
      처음=False : 장중에 새로 상장됐거나 밖에서 기준가·업종을 바꾼 경우. 지금 가격에서 이어 간다.
                   위치 = 이 종목의 기준가가 적힌 시점의 지수·업종 장중 등락 (_장["M"])
    돌려주는 값 = 시작 가격(로그)
    """
    심볼, k = 행["sym"], str(행["시장"])
    옛 = _종목.get(심볼) or {}
    업 = 행.get("업종")
    유전, 베타, 변동, 감마 = _성격(심볼, 행["시장"], 업)
    L = math.log(행["base"])
    # u 업종, g 업종 민감도. 물려받은 실제 종목이 바뀌었으면 변동성 기억(s2)도 새로 시작한다
    st = {"날짜": 날짜, "시장": 행["시장"], "b": 베타, "v": 변동, "u": 업, "g": 감마, "유전": 유전,
          "s2": (옛.get("s2") if 옛.get("유전") == 유전 else None) or 변동 ** 2, "pc": L, "끝": False}

    s = math.sqrt(st["s2"]) * 변동성_배율
    zg, zn, zr = random.choice(자료()["잔차"][k])
    if 처음:
        갭 = 베타 * 장["갭"][k] + 감마 * 장["갭"].get(업, 0.0) + math.sqrt(st["s2"]) * 갭_배율 * zg
        if 행.get("배당락"):
            갭 += math.log(max(0.01, 1 - 행["배당락"]))      # 어제 배당을 받을 권리가 떨어져 나간 만큼 낮게 시작
        시작 = L + max(math.log(1 - 가격제한폭), min(math.log(1 + 가격제한폭), 갭))
        st["o"] = 시작
        st["tgt"] = s * zn
        지난 = 0.0
    else:
        지난 = 장["tau"]
        시작 = L
        위치 = 위치 or {}
        st["o"] = L - 베타 * 위치.get(k, 0.0) - 감마 * 위치.get(업, 0.0)      # 지금 위치가 '고유 등락 0' 이 되게
        st["tgt"] = s * zn * math.sqrt(max(0.0, 1 - 지난))
    st["nv"] = s * max(zr, _잡음_최소)
    st["vi기준"] = 시작
    st["실적발표"] = 옛.get("실적발표")
    if 실적날인지(심볼, 날짜):
        st["실적일"] = 날짜
    if st.get("실적일") == 날짜 and st["실적발표"] != 날짜 and 지난 < 0.85:
        # 실적 발표날: 고유 등락을 크게 잡고, 그 대부분이 발표 시각에 한 번에 터진다
        st["tgt"] *= 실적_변동배율
        J = st["tgt"] * random.uniform(0.7, 0.95)
        st["뉴스"] = {"tau": random.uniform(max(0.05, 지난 + 0.02), 0.85), "J": J, "실적": True,
                    "z": J / max(s * math.sqrt(max(0.05, 1 - 지난)), 1e-9)}
    else:
        _뉴스계획(st, 지난)
    _종목[심볼] = st
    return 시작


def _가격쓰기(cursor, 행, L, 지금, 거래량=0.0):
    """기준가(로그) → DB. 돌려주는 값 = 새 주가"""
    import tools
    하한 = tools.시장_하한.get(행["시장"], 500)
    기준 = max(float(하한), math.exp(L))
    글 = f"{기준:.6f}"
    가격 = tools.가격계산(int(기준), 행["시장"], 행["held"], 행["supply"])
    분 = 지금.strftime("%H:%M")
    if 글 != 행["글"] or 가격 != 행["price"]:
        cursor.execute("UPDATE stocks SET price = ?, base_price = ? WHERE symbol = ?",
                       (str(가격), 글, 행["sym"]))
    if 가격 != 행["price"] or _쓴분.get(행["sym"]) != 분 or 거래량 > 0:
        캔들기록(cursor, 행["sym"], 가격, 지금, 거래량)
        _쓴분[행["sym"]] = 분
    _쓴값[행["sym"]] = 글
    행["base"], 행["글"], 행["price"] = 기준, 글, 가격
    return 가격


def _개장(cursor, 날짜, 행들, 지금, 시작=0.0):
    """
    오늘 장을 연다. 시작 = 장을 여는 시점의 변동 진행률.
    0 이면 정상 개장 (전일 종가에서 갭). 0 보다 크면 엔진을 처음 켠 날이라 갭 없이 지금 가격에서 이어 간다.
    """
    global _장
    d = 자료()
    지수 = d["지수"]
    블록 = (_장 or {}).get("블록") or [0, 0]
    if 블록[1] <= 0:
        블록 = [random.randrange(len(지수)), random.randint(5, 20)]     # 실제 5~20거래일을 이어서 쓴다
    else:
        블록 = [(블록[0] + 1) % len(지수), 블록[1] - 1]
    _, kg, kn, kp, qg, qn, qp = 지수[블록[0]]
    if 장기추세_제거:
        kn, qn = kn - d["추세"]["1"], qn - d["추세"]["0"]
    배 = 변동성_배율
    남 = math.sqrt(1 - 시작)
    _장 = {
        "날짜": 날짜, "tau": 시작, "마감": False, "블록": 블록,
        "갭": {"1": kg * 갭_배율 if not 시작 else 0.0, "0": qg * 갭_배율 if not 시작 else 0.0},
        "목표": {"1": kn * 배 * 남, "0": qn * 배 * 남},
        "잡음": {"1": max(kp, _지수잡음_최소) * 배, "0": max(qp, _지수잡음_최소) * 배},
        "M": {"1": 0.0, "0": 0.0},
    }
    # 업종: 같은 날짜의 실제 업종 움직임(지수로 설명 안 되는 몫). 지수와 같은 칸에 업종 이름으로 넣는다
    세기 = max(0.6, min(2.5, (kp + qp) / 2 / d["평균폭"])) if d["평균폭"] > 0 else 1.0     # 시장이 출렁인 날은 업종도 출렁인다
    for 업, 정보 in d["업종"].items():
        줄 = d["업종날"].get(업) or []
        ug, un = 줄[블록[0]] if 블록[0] < len(줄) else (0.0, 0.0)
        if 장기추세_제거:
            un -= 정보.get("추세", 0.0)
        _장["갭"][업] = ug * 갭_배율 if not 시작 else 0.0
        _장["목표"][업] = un * 배 * 남
        _장["잡음"][업] = 정보.get("잡음", 0.0) * 세기 * 배
        _장["M"][업] = 0.0
    # 배당락: 어제(또는 그 전) 기준일에 배당을 준 종목은 그만큼 낮게 시작한다
    cursor.execute("SELECT symbol, MAX(rate) FROM stock_dividends WHERE ex_done = 0 AND date < ? GROUP BY symbol", (날짜,))
    배당락 = {심볼: 비율 or 0.0 for 심볼, 비율 in cursor.fetchall()}
    cursor.execute("UPDATE stock_dividends SET ex_done = 1 WHERE ex_done = 0 AND date < ?", (날짜,))
    있는 = set()
    for 행 in 행들:
        있는.add(행["sym"])
        행["배당락"] = 배당락.get(행["sym"], 0.0)
        시가 = _새하루(행, 날짜, _장, 처음=not 시작)
        _가격쓰기(cursor, 행, 시가, 지금)
        _저장(cursor, 행["sym"], _종목[행["sym"]])
    for 심볼 in [s for s in _종목 if s not in 있는]:        # 상장폐지된 종목
        del _종목[심볼]
        cursor.execute("DELETE FROM stock_sim WHERE symbol = ?", (심볼,))


def _정지중(끝, 지금):
    """끝(ISO 시각 글자)이 아직 안 왔으면 True"""
    if not 끝:
        return False
    try:
        return 지금 < datetime.fromisoformat(끝)
    except ValueError:
        return False


def _서킷확인(지금, 닫):
    """지수가 많이 빠졌으면 그 시장 전체를 멈추고(서킷브레이커), 시간이 다 됐으면 푼다. 알림 목록을 돌려준다."""
    알림 = []
    cb = _장.setdefault("cb", {})
    단계 = _장.setdefault("cb단계", {})
    for k in ("1", "0"):
        if cb.get(k):
            if _정지중(cb[k], 지금):
                continue
            cb[k] = None
            if 지금 < 닫:
                알림.append({"종류": "서킷해제", "시장": int(k), "단계": 단계.get(k, 0)})
        지수 = _장["갭"][k] + _장["M"][k]
        n = 단계.get(k, 0)
        새 = n
        for i, (폭, 분) in enumerate(서킷_단계):
            if i >= n and 지수 <= math.log(1 - 폭):
                새 = i + 1
        if 새 == n:
            continue
        폭, 분 = 서킷_단계[새 - 1]
        if 분 is not None and (닫 - 지금).total_seconds() < 서킷_막판분 * 60:
            continue                     # 막판에는 1·2단계를 걸지 않는다 (다음 틱에 다시 보지 않게 단계만 올린다)
        단계[k] = 새
        끝 = 닫 if 분 is None else min(닫, 지금 + timedelta(minutes=분))
        cb[k] = 끝.isoformat(timespec="seconds")
        알림.append({"종류": "서킷", "시장": int(k), "단계": 새, "지수": math.exp(지수) - 1, "분": 분,
                    "끝": cb[k]})
    return 알림


def _진행(cursor, 행들, tau, 지금, 닫=None):
    """
    변동 진행률을 _장['tau'] → tau 로 옮기며 모든 종목을 한 걸음 움직인다. 알림 목록을 돌려준다.
    VI·서킷브레이커로 멈춘 종목은 가격을 쓰지 않고 속으로만 움직이다가(vL), 풀릴 때 그 가격으로 한 번에 간다 (단일가 매매처럼).
    """
    import tools
    이전 = _장["tau"]
    d = tau - 이전
    if d <= 0:
        return []
    닫 = 닫 or 장시간(지금)[1]
    마지막 = tau >= 1.0
    남은 = 1 - 이전
    흔들 = math.sqrt(max(0.0, d * (남은 - d) / 남은))       # 브리지: 끝에 가까울수록 흔들림이 줄어 목표에 닿는다
    옛M = dict(_장["M"])
    for k in 옛M:                       # 지수 둘("1", "0") + 업종들
        _장["M"][k] = 옛M[k] + (_장["목표"][k] - 옛M[k]) * d / 남은 + _장["잡음"][k] * 흔들 * random.gauss(0, 1)

    알림 = _서킷확인(지금, 닫)
    cb = _장.get("cb") or {}
    상, 하 = math.log(1 + 가격제한폭), math.log(1 - 가격제한폭)
    vi상, vi하 = math.log(1 + VI_기준), math.log(1 - VI_기준)
    for 행 in 행들:
        심볼, k = 행["sym"], str(행["시장"])
        st = _종목.get(심볼)
        밖에서바꿈 = 심볼 in _쓴값 and _쓴값[심볼] != 행["글"]
        바뀜 = False
        업종바뀜 = st is not None and "u" in st and st["u"] != 행["업종"]
        if st is None or st.get("날짜") != _장["날짜"] or 밖에서바꿈 or 업종바뀜:
            _새하루(행, _장["날짜"], _장, 처음=False, 위치=옛M)
            st = _종목[심볼]
            바뀜 = True

        # 업종 없이 세운 계획(업종 기능을 넣기 전에 연 장)은 그날 끝까지 업종 없이 간다
        업, 감마 = st.get("u"), st.get("g", 0.0)
        지금L = math.log(행["base"])
        L = st["vL"] if st.get("vL") is not None else 지금L       # 멈춰 있으면 속으로 움직이던 가격에서 잇는다
        고유 = L - st["o"] - st["b"] * 옛M[k] - 감마 * 옛M.get(업, 0.0)
        뉴스 = st.get("뉴스")
        목표 = st["tgt"] - (뉴스["J"] if 뉴스 else 0.0)
        고유 += (목표 - 고유) * d / 남은 + st["nv"] * 흔들 * random.gauss(0, 1)
        터짐 = None
        if 뉴스 and tau >= 뉴스["tau"]:
            고유 += 뉴스["J"]
            터짐 = 뉴스
            st["뉴스"] = None
            if 뉴스.get("실적"):
                st["실적발표"] = _장["날짜"]
            바뀜 = True

        새L = st["o"] + st["b"] * _장["M"][k] + 감마 * _장["M"].get(업, 0.0) + 고유
        새L = max(st["pc"] + 하, min(st["pc"] + 상, 새L))
        이전가 = 행["price"]

        서킷 = cb.get(k)
        멈춤 = (_정지중(서킷, 지금) or _정지중(st.get("vi"), 지금)) and not 마지막
        if 마지막 and 서킷 and 서킷단계(k) >= len(서킷_단계):
            멈춤 = True                  # 3단계 서킷브레이커는 그날 장이 끝난다. 멈춘 가격이 종가
        vi발동 = vi해제 = None
        if 멈춤:
            st["vL"] = 새L
            바뀜 = True
            쓸L = None
        else:
            쓸L = 새L
            if st.get("vL") is not None:              # 방금 풀렸다 → 속으로 움직인 가격으로 한 번에
                vi해제 = bool(st.get("vi"))
                st["vL"] = st["vi"] = None
                st["vi기준"] = 새L
                바뀜 = True
            elif not 마지막:
                기준 = st.get("vi기준", st["o"])
                if 새L - 기준 >= vi상 or 새L - 기준 <= vi하:
                    위로 = 새L > 기준
                    쓸L = 기준 + (vi상 if 위로 else vi하)     # VI 가 걸린 가격에서 멈춘다
                    st["vL"] = 새L
                    st["vi"] = (지금 + timedelta(minutes=VI_분)).isoformat(timespec="seconds")
                    vi발동 = "상승" if 위로 else "하락"
                    바뀜 = True

        새가격 = 이전가
        if 쓸L is not None:
            기대 = max(st["nv"] * math.sqrt(d), 1e-6)
            활발 = min(20.0, 0.5 + 0.5 * abs(쓸L - L) / 기대) * (2.0 if st.get("실적일") == _장["날짜"] else 1.0)
            거래량 = 행["supply"] * 회전율.get(행["시장"], 0.2) * d * 활발 * random.lognormvariate(-0.045, 0.3)
            새가격 = _가격쓰기(cursor, 행, 쓸L, 지금, 거래량)
        등락 = math.exp((쓸L if 쓸L is not None else 지금L) - st["pc"]) - 1

        한계 = None
        if 쓸L is not None:
            if 쓸L >= st["pc"] + 상 - 1e-9 and not st.get("상한"):
                st["상한"], 한계, 바뀜 = True, "상한가", True
            elif 쓸L <= st["pc"] + 하 + 1e-9 and not st.get("하한") \
                    and 행["base"] > tools.시장_하한.get(행["시장"], 500) + 1:
                st["하한"], 한계, 바뀜 = True, "하한가", True
        if 터짐 is not None or 한계 or vi발동 or vi해제:
            종류 = None
            if 터짐 is not None:
                종류 = "실적" if 터짐.get("실적") else "호재" if 터짐["J"] > 0 else "악재"
            알림.append({"종류": 종류, "한계": 한계, "vi": vi발동, "vi해제": bool(vi해제),
                        "vi끝": st.get("vi") if vi발동 else None,
                        "실적": _실적평가(터짐["z"]) if 터짐 is not None and 터짐.get("실적") else None,
                        "멈춤": 멈춤, "name": 행["name"], "symbol": 심볼,
                        "이전가": 이전가, "새가격": 새가격, "등락": 등락})
        if 바뀜:
            _저장(cursor, 심볼, st)
    _장["tau"] = tau
    return 알림


def _실적평가(z):
    """발표날 움직임(그날 표준편차 대비) → 실적 평가 문구"""
    if z >= 1.0:
        return "어닝 서프라이즈"
    if z <= -1.0:
        return "어닝 쇼크"
    if z >= 0.3:
        return "예상 소폭 상회"
    if z <= -0.3:
        return "예상 소폭 하회"
    return "예상 부합"


def 서킷단계(k):
    return ((_장 or {}).get("cb단계") or {}).get(str(k), 0)


def _마감(cursor, 행들):
    for 행 in 행들:
        st = _종목.get(행["sym"])
        if st and st.get("날짜") == _장["날짜"]:
            k = str(행["시장"])
            업 = st.get("u")
            st["지수"] = _장["갭"][k] + _장["M"][k]
            st["업종등락"] = _장["갭"].get(업, 0.0) + _장["M"].get(업, 0.0)
            _하루끝(st, math.log(행["base"]), st["b"] * st["지수"] + st.get("g", 0.0) * st["업종등락"])
            _저장(cursor, 행["sym"], st)
    _장["마감"] = True


def 틱(conn, cursor, 지금=None):
    """
    주가를 한 걸음 움직인다. 봇의 주가변동 루프가 틱_초마다 부른다.
    장이 닫혀 있으면 아무것도 안 한다. 봇이 잠깐 꺼졌다 켜져도 그동안의 시간만큼 한 번에 움직인다.
    돌려주는 값: 알림 목록 [{종류: 호재/악재/None, 한계: 상한가/하한가/None, name, symbol, 이전가, 새가격, 등락(전일 대비)}]
    """
    지금 = 지금 or datetime.now()
    if not _불러옴:
        _불러오기(cursor)
    if 지금.weekday() in 휴장요일:
        return []
    열, 닫 = 장시간(지금)
    if 지금 < 열:
        return []
    날짜 = f"{지금:%Y-%m-%d}"
    새날 = _장 is None or _장.get("날짜") != 날짜
    if (새날 and 지금 >= 닫) or (not 새날 and _장.get("마감")):
        return []

    행들 = _종목행(cursor)
    끝 = 지금 >= 닫
    기록시각 = 닫 - timedelta(seconds=1) if 끝 else 지금       # 늦게 마감해도 오늘 마지막 봉에 들어가게
    tau = _타우((min(지금, 닫) - 열) / (닫 - 열))
    if 새날:
        if _장 and not _장.get("마감"):
            _마감(cursor, 행들)          # 어제 마감 때 봇이 꺼져 있었으면 지금 한다
        # 엔진을 처음 켠 날 장중이면 지금 가격에서 조용히 이어 간다 (하루치 등락이 한 번에 터지지 않게)
        첫날 = _장 is None and (지금 - 열).total_seconds() > 120 and not 끝
        개장전(cursor, 날짜)      # 갭을 쓰기 전 = 전일 종가에서 ETF 기준을 잡는다
        _개장(cursor, 날짜, 행들, 기록시각, tau if 첫날 else 0.0)
    알림 = _진행(cursor, 행들, tau, 기록시각, 닫)
    진행(cursor, 기록시각, tau, 날짜)
    if 끝:
        _마감(cursor, 행들)
    _저장(cursor, "*", _장)
    conn.commit()
    return 알림


def 종목삭제(cursor, 심볼):
    """상장폐지할 때 엔진 기억과 차트를 지운다. 커밋은 부른 쪽에서."""
    _종목.pop(심볼, None)
    _쓴값.pop(심볼, None)
    _쓴분.pop(심볼, None)
    cursor.execute("DELETE FROM stock_sim WHERE symbol = ?", (심볼,))
    cursor.execute("DELETE FROM stock_candles WHERE symbol = ?", (심볼,))
    cursor.execute("UPDATE stock_etfs SET removed = 1 WHERE symbol = ?", (심볼,))


def 다시읽기():
    """틱이 중간에 실패했을 때 부른다. 다음 틱이 DB에 저장된 상태에서 다시 시작한다."""
    global _장, _불러옴
    _장 = None
    _종목.clear()
    _쓴값.clear()
    _쓴분.clear()
    _불러옴 = False


# ============================================================
# 거래 정지 조회 (봇·웹 공용 — DB 에서 읽는다)
# ============================================================
def _남은글(초):
    초 = max(1, int(초))
    return f"{초 // 60}분 {초 % 60}초" if 초 >= 60 else f"{초}초"


def 정지목록(cursor, 지금=None):
    """{심볼: {"종류": "VI"/"서킷", "끝": ISO, "남은초": n, "글": 안내}} — 지금 멈춰 있는 종목만"""
    지금 = 지금 or datetime.now()
    cursor.execute("SELECT symbol, data FROM stock_sim")
    장, 종목들 = None, {}
    for 심볼, 글 in cursor.fetchall():
        try:
            값 = json.loads(글)
        except ValueError:
            continue
        if 심볼 == "*":
            장 = 값
        else:
            종목들[심볼] = 값
    결과 = {}
    오늘 = f"{지금:%Y-%m-%d}"
    cb = (장 or {}).get("cb") or {} if (장 or {}).get("날짜") == 오늘 else {}
    for 심볼, st in 종목들.items():
        if st.get("날짜") != 오늘:
            continue
        서킷 = cb.get(str(st.get("시장")))
        if _정지중(서킷, 지금):
            끝 = datetime.fromisoformat(서킷)
            시장 = "코스피" if str(st.get("시장")) == "1" else "코스닥"
            단 = ((장 or {}).get("cb단계") or {}).get(str(st.get("시장")), 0)
            if 단 >= len(서킷_단계):
                글 = f"🚨 {시장} 서킷브레이커 {단}단계 발동 — 오늘 {시장} 거래는 끝났어요"
            else:
                글 = f"🚨 {시장} 서킷브레이커 {단}단계 발동 중 — {_남은글((끝 - 지금).total_seconds())} 뒤 거래 재개"
            결과[심볼] = {"종류": "서킷", "끝": 서킷, "남은초": int((끝 - 지금).total_seconds()), "글": 글}
        elif _정지중(st.get("vi"), 지금):
            끝 = datetime.fromisoformat(st["vi"])
            결과[심볼] = {"종류": "VI", "끝": st["vi"], "남은초": int((끝 - 지금).total_seconds()),
                        "글": f"⏸️ VI(변동성 완화장치) 발동 중 — {_남은글((끝 - 지금).total_seconds())} 뒤 거래 재개"}
    if any(_정지중(v, 지금) for v in cb.values()):          # ETF 는 VI 없이 자기 시장 서킷브레이커만 따른다
        cursor.execute("SELECT e.symbol, s.is_premium FROM stock_etfs e JOIN stocks s ON s.symbol = e.symbol "
                       "WHERE e.removed = 0")
        for 심볼, 시장 in cursor.fetchall():
            if 심볼 in 결과:
                continue
            서킷 = cb.get(str(시장))
            if _정지중(서킷, 지금):
                끝 = datetime.fromisoformat(서킷)
                이름 = "코스피" if 시장 == 1 else "코스닥"
                결과[심볼] = {"종류": "서킷", "끝": 서킷, "남은초": int((끝 - 지금).total_seconds()),
                            "글": f"🚨 {이름} 서킷브레이커 발동 중 — {_남은글((끝 - 지금).total_seconds())} 뒤 거래 재개"}
    return 결과


def 거래정지(cursor, 심볼, 지금=None):
    """이 종목이 VI·서킷브레이커로 멈춰 있으면 안내 문구, 아니면 None"""
    지금 = 지금 or datetime.now()
    cursor.execute("SELECT symbol, data FROM stock_sim WHERE symbol IN (?, '*')", (심볼,))
    행 = dict(cursor.fetchall())
    오늘 = f"{지금:%Y-%m-%d}"
    try:
        장 = json.loads(행.get("*") or "{}")
        if 심볼 in 행:
            st = json.loads(행[심볼])
        else:                                   # ETF: VI 는 없고 자기 시장 서킷브레이커만 따른다
            cursor.execute("SELECT s.is_premium FROM stock_etfs e JOIN stocks s ON s.symbol = e.symbol "
                           "WHERE e.symbol = ? AND e.removed = 0", (심볼,))
            r = cursor.fetchone()
            if r is None:
                return None
            st = {"날짜": 오늘, "시장": r[0]}
    except ValueError:
        return None
    if st.get("날짜") != 오늘:
        return None
    k = str(st.get("시장"))
    서킷 = ((장.get("cb") or {}).get(k)) if 장.get("날짜") == 오늘 else None
    시장 = "코스피" if k == "1" else "코스닥"
    if _정지중(서킷, 지금):
        단 = (장.get("cb단계") or {}).get(k, 0)
        if 단 >= len(서킷_단계):
            return f"🚨 {시장} 서킷브레이커 {단}단계가 발동돼 오늘 {시장} 거래는 끝났어요."
        끝 = datetime.fromisoformat(서킷)
        return f"🚨 {시장} 서킷브레이커 {단}단계가 발동돼 {시장} 전체 거래가 멈췄어요. {_남은글((끝 - 지금).total_seconds())} 뒤 다시 열려요."
    if _정지중(st.get("vi"), 지금):
        끝 = datetime.fromisoformat(st["vi"])
        return (f"⏸️ 주가가 급하게 움직여 VI(변동성 완화장치)가 발동됐어요. "
                f"{_남은글((끝 - 지금).total_seconds())} 뒤 다시 거래할 수 있어요.")
    return None


def 마감한날():
    """엔진이 오늘 장을 마감했으면 그 날짜 글자, 아니면 None (봇이 배당 처리 때 본다)"""
    return _장["날짜"] if _장 and _장.get("마감") else None


# ============================================================
# 실적 발표 일정 — 종목·분기마다 시즌 안의 하루로 정해진다 (미리 알 수 있다)
# ============================================================
def _시즌날들(연도, i):
    시작월, 시작일, 끝월, 끝일 = 실적_시즌[i]
    a = datetime(연도, 시작월, 시작일)
    b = datetime(연도, 끝월, 끝일)
    return [a + timedelta(days=n) for n in range((b - a).days + 1) if (a + timedelta(days=n)).weekday() not in 휴장요일]


def 실적일(심볼, 연도, i):
    """i 번째 시즌(0~3)에 이 종목이 실적을 발표하는 날 (datetime, 0시)"""
    날들 = _시즌날들(연도, i)
    h = int(hashlib.md5(f"실적|{심볼}|{연도}|{i}".encode()).hexdigest(), 16)
    return 날들[h % len(날들)]


def 실적날인지(심볼, 날짜):
    """날짜(글자 'YYYY-MM-DD')가 이 종목의 실적 발표날이면 True"""
    try:
        d = datetime.strptime(날짜, "%Y-%m-%d")
    except ValueError:
        return False
    for i in range(len(실적_시즌)):
        if 실적일(심볼, d.year, i) == d:
            return True
    return False


def 다음실적(심볼, 지금=None):
    """오늘 포함 다음 실적 발표날 (datetime). 오늘이면 오늘."""
    지금 = 지금 or datetime.now()
    오늘 = datetime(지금.year, 지금.month, 지금.day)
    후보 = [실적일(심볼, 연, i) for 연 in (지금.year, 지금.year + 1) for i in range(len(실적_시즌))]
    return min(d for d in 후보 if d >= 오늘)


def 실적일정(심볼들, 날수=14, 지금=None):
    """[(datetime, 심볼)] 오늘부터 날수 안에 발표하는 것, 날짜순"""
    지금 = 지금 or datetime.now()
    오늘 = datetime(지금.year, 지금.month, 지금.day)
    끝 = 오늘 + timedelta(days=날수)
    목록 = []
    for 심볼 in 심볼들:
        d = 다음실적(심볼, 지금)
        if d < 끝:
            목록.append((d, 심볼))
    return sorted(목록)


# ============================================================
# 2. ETF — 구성·NAV·병합
# ============================================================
# ETF — 여러 종목을 한 번에 담은 상품. 봇·웹·AI 툴이 같이 쓴다.
#
#   기초지수 : 규칙(시장·업종)에 맞는 종목을 같은 비중으로 담는다 (동일가중). 구성은 매일 개장 때 다시 정한다.
#              새로 상장한 종목은 다음 날부터 들어가고, 장중에 상장폐지된 종목은 마지막 가격으로 굳혔다가 다음 날 빠진다.
#   가격     : 매일 전일 종가에서 다시 시작한다 (실제 레버리지·인버스 ETF 처럼 '하루' 수익률에 배율을 곱한다).
#                NAV = 전일 NAV × (1 + 배율 × 구성 종목 평균 등락)      ← 등락은 기준가(base_price) 기준
#              그래서 레버리지·인버스를 오래 들고 있으면 출렁이는 장에서 손해가 쌓인다 (음의 복리, 실제와 같다).
#   총보수   : 연 보수를 날마다 NAV 에서 조금씩 뺀다.
#   매매     : LP(유동성 공급자)가 있다고 보고 수급 충격 없이 NAV 그대로 사고판다. 유통량·지분 한도 없음 (tools.py).
#   세금     : 국내 주식형 ETF 처럼 거래세·양도세 없음. 분배금(배당)만 배당소득세.
#   분배금   : 1배 ETF 만, 구성 종목 배당률 평균 (배당 칸). 구성 종목이 배당락으로 내려가는 만큼 NAV 도 같이 내려간다.
#   병합     : NAV 가 병합_기준 아래로 내려가면 장이 닫혀 있을 때 병합_배수 주를 1주로 합친다 (남는 주는 현금으로).
#
# 엔진(틱)이 새 날 개장 직전에 개장전(), 틱마다 진행() 을 부른다 (엔진 연결).
# 병합확인() 은 봇 루프가 tools 연결로 부른다.

# ============================================================
# 설정
# ============================================================
# (코드, 이름, 상장 시장 1=코스피 0=코스닥, 배율, 규칙, 연 총보수, 설명)
#   규칙: {"시장": 1} 그 시장 전 종목 / {"업종": [...]} 그 업종들 (시장 안 가림) / 둘 다 쓰면 둘 다 맞아야
# 처음 켤 때 없는 것만 상장한다. 상장폐지(!상장폐지)한 ETF 는 다시 안 만든다.
ETF목록 = (
    ("KOSPI", "우리코스피", 1, 1, {"시장": 1}, 0.0015, "코스피 전 종목 동일가중"),
    ("KOSPI2X", "우리코스피레버리지", 1, 2, {"시장": 1}, 0.0064, "코스피 하루 등락의 2배"),
    ("KOSPIINV", "우리코스피인버스", 1, -1, {"시장": 1}, 0.0064, "코스피 하루 등락의 반대"),
    ("KOSPIINV2X", "우리코스피곱버스", 1, -2, {"시장": 1}, 0.0064, "코스피 하루 등락의 반대 2배"),
    ("KOSDAQ", "우리코스닥", 0, 1, {"시장": 0}, 0.0025, "코스닥 전 종목 동일가중"),
    ("KOSDAQ2X", "우리코스닥레버리지", 0, 2, {"시장": 0}, 0.0064, "코스닥 하루 등락의 2배"),
    ("KOSDAQINV", "우리코스닥인버스", 0, -1, {"시장": 0}, 0.0064, "코스닥 하루 등락의 반대"),
    ("SEMIETF", "우리반도체전자", 1, 1, {"업종": ["반도체", "전자"]}, 0.0045, "반도체·전자 업종"),
    ("ITETF", "우리인터넷IT", 1, 1, {"업종": ["IT"]}, 0.0045, "IT 업종"),
    ("GAMEETF", "우리게임엔터", 1, 1, {"업종": ["게임", "엔터"]}, 0.0045, "게임·엔터 업종"),
    ("FOODETF", "우리식품유통", 1, 1, {"업종": ["식품", "유통"]}, 0.0045, "식품·유통 업종"),
)
시작가 = 10_000
병합_기준 = 1_000           # NAV 가 이보다 낮아지면 병합
병합_배수 = 10              # 이 주 수를 1주로
거래량_기본 = 3_000         # 1배 지수 ETF 하루 거래량(주) 기준. 레버리지·인버스는 2배, 업종 ETF 는 절반

# 엔진·다른 모듈이 'ETF 가 아닌 종목' 을 고를 때 붙이는 조건
아님 = "symbol NOT IN (SELECT symbol FROM stock_etfs WHERE removed = 0)"


# ============================================================
# 정의 읽기
# ============================================================
def _정의(행):
    심볼, 이름, 시장, 기준, 가격, 배율, 규칙, 보수, 설명, 상태 = 행
    try:
        규칙 = json.loads(규칙 or "{}")
    except ValueError:
        규칙 = {}
    try:
        상태 = json.loads(상태) if 상태 else {}
    except ValueError:
        상태 = {}
    return {"symbol": 심볼, "name": 이름, "market": 시장, "lev": 배율, "rule": 규칙, "fee": 보수 or 0.0,
            "desc": 설명 or "", "state": 상태, "base": float(기준 or 0) or float(가격 or 0) or float(시작가),
            "price": int(float(가격 or 0))}


_ETF열 = ("SELECT e.symbol, s.name, s.is_premium, s.base_price, s.price, e.lev, e.rule, e.fee, e.descr, e.state "
      "FROM stock_etfs e JOIN stocks s ON s.symbol = e.symbol WHERE e.removed = 0")


def 정의들(cursor):
    """{심볼: 정의} — 지금 상장돼 있는 ETF"""
    cursor.execute(_ETF열)
    순서 = {x[0]: i for i, x in enumerate(ETF목록)}
    행들 = sorted(cursor.fetchall(), key=lambda r: (순서.get(r[0], len(순서)), r[0]))
    return {r[0]: _정의(r) for r in 행들}


def 정의(cursor, 심볼):
    cursor.execute(_ETF열 + " AND e.symbol = ?", (심볼,))
    r = cursor.fetchone()
    return _정의(r) if r else None


def 배율이름(배율):
    배율 = int(배율) if float(배율).is_integer() else 배율
    return {1: "1배", 2: "레버리지 2배", -1: "인버스", -2: "인버스 2배"}.get(배율, f"{배율}배")


def 지수이름(규칙):
    if "업종" in 규칙:
        글 = "·".join(규칙["업종"]) + " 업종"
        if "시장" in 규칙:
            글 = ("코스피 " if 규칙["시장"] == 1 else "코스닥 ") + 글
        return 글
    if "시장" in 규칙:
        return "코스피 전 종목" if 규칙["시장"] == 1 else "코스닥 전 종목"
    return "전 종목"


def _종목들(cursor):
    """ETF 가 아닌 코스피·코스닥 종목 {심볼: (이름, 시장, 업종, 기준가)}"""
    cursor.execute(f"SELECT symbol, name, is_premium, sector, base_price, price FROM stocks "
                   f"WHERE is_premium IN (0, 1) AND {아님}")
    결과 = {}
    for 심볼, 이름, 시장, 업종, 기준, 가격 in cursor.fetchall():
        try:
            b = float(기준 or 0) or float(가격 or 0)
        except ValueError:
            b = 0.0
        결과[심볼] = (이름, 시장, 종목업종(업종, 이름), b)
    return 결과


def 구성(정의, 종목들):
    """규칙에 맞는 지금 종목 심볼 목록"""
    규칙 = 정의["rule"]
    결과 = []
    for 심볼, (_, 시장, 업종, 기준) in 종목들.items():
        if "시장" in 규칙 and 시장 != 규칙["시장"]:
            continue
        if "업종" in 규칙 and 업종 not in 규칙["업종"]:
            continue
        if 기준 > 0:
            결과.append(심볼)
    return sorted(결과)


# ============================================================
# 상장 (봇이 켤 때 한 번)
# ============================================================
def 준비(conn, cursor):
    """ETF목록 중 아직 없는 것을 상장한다. 코드가 이미 다른 종목에 쓰이고 있으면 건너뛴다."""
    cursor.execute("SELECT symbol FROM stock_etfs")
    있는 = {r[0] for r in cursor.fetchall()}
    지금 = datetime.now()
    새로 = []
    for 코드, 이름, 시장, 배율, 규칙, 보수, 설명 in ETF목록:
        if 코드 in 있는:
            continue
        cursor.execute("SELECT 1 FROM stocks WHERE symbol = ? OR name = ?", (코드, 이름))
        if cursor.fetchone():
            print(f"[{지금}] ETF {코드}: 같은 코드·이름의 종목이 있어 상장하지 않음")
            continue
        cursor.execute("INSERT INTO stocks (symbol, name, price, is_premium, base_price, supply, sector) "
                       "VALUES (?, ?, ?, ?, ?, '0', NULL)", (코드, 이름, str(시작가), 시장, f"{시작가:.6f}"))
        cursor.execute("INSERT INTO stock_etfs (symbol, lev, rule, fee, descr, created) VALUES (?, ?, ?, ?, ?, ?)",
                       (코드, 배율, json.dumps(규칙, ensure_ascii=False), 보수, 설명, 지금.isoformat(timespec="seconds")))
        캔들기록(cursor, 코드, 시작가, 지금)
        새로.append(코드)
    conn.commit()
    if 새로:
        print(f"[{지금}] ETF 상장: {', '.join(새로)}")
    return 새로


# ============================================================
# 가격 (엔진이 부른다. 커밋은 엔진이)
# ============================================================
def _하루시작(정의, 날짜, 종목들):
    """오늘 계획: 전일 NAV(보수 뺀 것)와 구성 종목들의 전일 기준가"""
    st = 정의["state"] or {}
    nav = 정의["base"]
    try:
        일수 = (datetime.strptime(날짜, "%Y-%m-%d") - datetime.strptime(st["날짜"], "%Y-%m-%d")).days
    except (KeyError, ValueError, TypeError):
        일수 = 0
    nav *= (1 - 정의["fee"]) ** (max(0, min(일수, 30)) / 365)
    목록 = 구성(정의, 종목들)
    return {"날짜": 날짜, "nav0": nav, "ref": {s: 종목들[s][3] for s in 목록}, "last": {}}


def 개장전(cursor, 날짜):
    """새 날 엔진이 갭을 쓰기 전에 부른다. 지금 기준가 = 전일 종가라서 여기서 구성·기준을 정한다."""
    종목들 = _종목들(cursor)
    for 정의 in 정의들(cursor).values():
        if (정의["state"] or {}).get("날짜") == 날짜:
            continue
        st = _하루시작(정의, 날짜, 종목들)
        cursor.execute("UPDATE stock_etfs SET state = ? WHERE symbol = ?",
                       (json.dumps(st, ensure_ascii=False), 정의["symbol"]))


def 지수등락(st, 종목들):
    """구성 종목 평균 등락 (전일 기준가 대비). st['last'] 를 고친다."""
    ref, last = st.get("ref") or {}, st.setdefault("last", {})
    합 = 0.0
    for 심볼, r0 in ref.items():
        b = 종목들[심볼][3] if 심볼 in 종목들 else last.get(심볼, r0)     # 상장폐지됐으면 마지막 가격으로 굳힌다
        last[심볼] = b
        합 += b / r0 - 1 if r0 else 0.0
    return 합 / len(ref) if ref else 0.0


def 진행(cursor, 지금, tau, 날짜):
    """엔진이 종목들을 움직인 뒤 부른다. ETF NAV 를 다시 계산해 기준가·현재가·캔들에 쓴다."""
    정의목록 = 정의들(cursor)
    if not 정의목록:
        return
    종목들 = _종목들(cursor)
    for 정의 in 정의목록.values():
        st = 정의["state"]
        if not st or st.get("날짜") != 날짜:
            st = _하루시작(정의, 날짜, 종목들)        # 장중에 처음 상장했거나 개장전을 놓쳤다
        r = 지수등락(st, 종목들)
        nav = max(st["nav0"] * 0.01, st["nav0"] * (1 + 정의["lev"] * r))
        이전 = 정의["base"]
        d = max(0.0, tau - st.get("tau", tau))
        st["tau"] = tau
        기대 = max(0.03 * abs(정의["lev"]) * math.sqrt(d), 1e-6)
        활발 = min(10.0, 0.5 + 0.5 * abs(math.log(nav / 이전)) / 기대) if 이전 > 0 else 1.0
        하루량 = 거래량_기본 * (2 if 정의["lev"] != 1 else 1) * (0.5 if "업종" in 정의["rule"] else 1)
        거래량 = 하루량 * d * 활발 * random.lognormvariate(-0.045, 0.3)
        가격 = max(1, round(nav))
        cursor.execute("UPDATE stocks SET base_price = ?, price = ? WHERE symbol = ?",
                       (f"{nav:.6f}", str(가격), 정의["symbol"]))
        cursor.execute("UPDATE stock_etfs SET state = ? WHERE symbol = ?",
                       (json.dumps(st, ensure_ascii=False), 정의["symbol"]))
        캔들기록(cursor, 정의["symbol"], 가격, 지금, 거래량)


# ============================================================
# 조회 (봇·웹·AI)
# ============================================================
def 상세(cursor, 심볼):
    """
    ETF 하나의 구성과 오늘 기여. 없으면 None
    {symbol, name, market, lev, levName, index, fee, desc, nav, change(오늘 NAV 등락), indexChange(구성 평균 등락),
     holdings: [{symbol, name, weight, change(전일 대비), contrib}]}
    """
    정 = 정의(cursor, 심볼)
    if 정 is None:
        return None
    종목들 = _종목들(cursor)
    st = 정["state"] or {}
    ref = st.get("ref")                # 마지막으로 연 장의 구성 (장이 닫혀 있으면 그날 등락 그대로)
    if not ref:
        ref = {s: 종목들[s][3] for s in 구성(정, 종목들)}
    n = len(ref) or 1
    목록 = []
    for s, r0 in ref.items():
        b = 종목들[s][3] if s in 종목들 else (st.get("last") or {}).get(s, r0)
        ch = b / r0 - 1 if r0 else 0.0
        목록.append({"symbol": s, "name": 종목들[s][0] if s in 종목들 else s, "weight": 1 / n,
                    "change": ch, "contrib": ch / n, "listed": s in 종목들})
    목록.sort(key=lambda x: -x["contrib"])
    지수 = sum(x["contrib"] for x in 목록)
    nav0 = st.get("nav0") or 정["base"]
    return {"symbol": 정["symbol"], "name": 정["name"], "market": 정["market"], "lev": 정["lev"],
            "levName": 배율이름(정["lev"]), "index": 지수이름(정["rule"]), "fee": 정["fee"], "desc": 정["desc"],
            "nav": 정["base"], "price": 정["price"], "change": 정["base"] / nav0 - 1 if nav0 else 0.0,
            "indexChange": 지수, "holdings": 목록, "count": len(목록)}


def 요약(cursor):
    """[{symbol, name, market, lev, levName, index, fee, count, nav, price}] — 목록용 (구성 수는 오늘 기준)"""
    종목들 = _종목들(cursor)
    결과 = []
    for 정 in 정의들(cursor).values():
        ref = (정["state"] or {}).get("ref")
        결과.append({"symbol": 정["symbol"], "name": 정["name"], "market": 정["market"], "lev": 정["lev"],
                    "levName": 배율이름(정["lev"]), "index": 지수이름(정["rule"]), "fee": 정["fee"],
                    "desc": 정["desc"], "count": len(ref) if ref else len(구성(정, 종목들)),
                    "nav": 정["base"], "price": 정["price"]})
    return 결과


def 분배율(cursor, 심볼, 종목배당률):
    """ETF 연 분배율 = 구성 종목 배당률 평균 (1배만). 종목배당률 = {심볼: 연율}. ETF 가 아니면 None"""
    정 = 정의(cursor, 심볼)
    if 정 is None:
        return None
    return _분배율(정, 종목배당률, _종목들(cursor))


def _분배율(정, 종목배당률, 종목들):
    if 정["lev"] != 1:
        return 0.0
    ref = (정["state"] or {}).get("ref") or {s: 0 for s in 구성(정, 종목들)}
    값들 = [종목배당률.get(s, 0.0) for s in ref if s in 종목들]
    return round(sum(값들) / len(값들), 4) if 값들 else 0.0


def 분배율표(cursor, 종목배당률):
    """{ETF 심볼: 연 분배율}"""
    종목들 = _종목들(cursor)
    return {s: _분배율(정, 종목배당률, 종목들) for s, 정 in 정의들(cursor).items()}


# ============================================================
# 병합 (봇 루프가 tools 연결로, 장이 닫혀 있을 때)
# ============================================================
def 병합확인():
    """NAV 가 병합_기준 아래인 ETF 를 병합한다. 돌려주는 값: [{symbol, name, k, before, after, users: {uid: 현금}}]"""
    import tools
    if 장중():
        return []
    c = tools.cursor
    결과 = []
    for 정 in 정의들(c).values():
        nav = 정["base"]
        if nav >= 병합_기준:
            continue
        k = 병합_배수
        심볼 = 정["symbol"]
        받은 = {}
        c.execute("SELECT user_id, CAST(quantity AS INTEGER), CAST(COALESCE(buyed_val, '0') AS INTEGER) "
                  "FROM portfolio WHERE symbol = ?", (심볼,))
        for uid, q, 매입 in c.fetchall():
            새 = q // k
            남 = q - 새 * k
            if 새 <= 0:
                c.execute("DELETE FROM portfolio WHERE user_id = ? AND symbol = ?", (uid, 심볼))
            else:
                c.execute("UPDATE portfolio SET quantity = ?, buyed_val = ? WHERE user_id = ? AND symbol = ?",
                          (str(새), str(매입 - (매입 * 남 // q if q else 0)), uid, 심볼))
            현금 = int(남 * nav)
            if 현금 > 0:
                tools.돈변경(uid, 현금, 커밋=False)
                받은[uid] = 현금
        # 걸린 주문: 가격 ×k, 수량 ÷k. 남는 수량이 0주가 되는 주문은 취소 (수량 0 = '전부' 매도 주문은 그대로)
        c.execute("UPDATE stock_orders SET status = 'canceled', done_at = ?, note = 'ETF 병합으로 남은 수량 0' "
                  "WHERE symbol = ? AND status = 'open' AND qty > 0 AND qty / ? <= filled / ?",
                  (datetime.now().isoformat(timespec="seconds"), 심볼, k, k))
        c.execute("UPDATE stock_orders SET price = price * ?, qty = qty / ?, filled = filled / ? "
                  "WHERE symbol = ? AND status = 'open'", (k, k, k, 심볼))
        # 차트는 수정주가처럼 지난 가격도 ×k
        c.execute("UPDATE stock_candles SET o = o * ?, h = h * ?, l = l * ?, c = c * ?, v = COALESCE(v, 0) / ? "
                  "WHERE symbol = ?", (k, k, k, k, k, 심볼))
        st = 정["state"] or {}
        if st.get("nav0"):
            st["nav0"] *= k
        c.execute("UPDATE stocks SET base_price = ?, price = ? WHERE symbol = ?",
                  (f"{nav * k:.6f}", str(max(1, round(nav * k))), 심볼))
        c.execute("UPDATE stock_etfs SET state = ? WHERE symbol = ?", (json.dumps(st, ensure_ascii=False), 심볼))
        tools.conn.commit()
        print(f"[{datetime.now()}] ETF 병합 {심볼} {k}:1 NAV {nav:,.0f} → {nav * k:,.0f}, 현금 정산 {len(받은)}명")
        결과.append({"symbol": 심볼, "name": 정["name"], "k": k, "before": nav, "after": nav * k, "users": 받은})
    return 결과


# ============================================================
# 3. 캔들 차트 그림 (!주식 그래프)
# ============================================================
# 주식·코인 그래프를 그림(PNG)으로 만든다. `!주식 그래프 [종목] [기간]`
#
# - 그리기()는 DB를 안 건드리니 run_in_executor 로 돌려도 된다.
# - 업비트자료()는 블로킹 HTTP 라 run_in_executor 로 부른다.
# - 색은 웹 차트와 같다: 오르면 빨강, 내리면 파랑.

# 구간 → (이름, 주식 봉, 코인 봉)
구간이름 = {
    "1h": ("1시간", "1분봉", "1분봉"),
    "1d": ("하루", "5분봉", "10분봉"),
    "1w": ("일주일", "1시간봉", "1시간봉"),
    "all": ("전체", "일봉", "일봉"),
}
_별칭 = {
    "1h": ("1h", "1시간", "한시간", "시간", "60분", "1분봉"),
    "1d": ("1d", "하루", "1일", "일", "오늘", "day", "5분봉"),
    "1w": ("1w", "일주일", "1주일", "1주", "주", "7일", "week", "시간봉"),
    "all": ("all", "전체", "전부", "일봉", "max"),
}
최대봉 = 300                 # 이보다 많으면 최근 것만 그린다 (봉 하나가 3픽셀은 돼야 보인다)


def 구간찾기(글):
    """'일주일' → '1w'. 기간이 아니면 None"""
    글 = (글 or "").strip().lower()
    for 키, 이름들 in _별칭.items():
        if 글 in 이름들:
            return 키
    return None


# ---------- 코인 (업비트) ----------
_업비트 = "https://api.upbit.com/v1"
_업비트캔들 = {"1h": ("minutes/1", 60), "1d": ("minutes/10", 144), "1w": ("minutes/60", 168), "all": ("days", 200)}


def 업비트자료(마켓, 구간):
    """블로킹. ([(시각 ISO, 시, 고, 저, 종)], 전일 대비 등락 비율 또는 None)"""
    종류, 개수 = _업비트캔들.get(구간, _업비트캔들["1d"])
    머리 = {"Accept": "application/json"}
    r = httpx.get(f"{_업비트}/candles/{종류}", params={"market": 마켓, "count": 개수}, headers=머리, timeout=6)
    r.raise_for_status()
    원 = lambda v: int(round(float(v)))       # noqa: E731
    봉들 = [(c["candle_date_time_kst"], 원(c["opening_price"]), 원(c["high_price"]),
             원(c["low_price"]), 원(c["trade_price"])) for c in reversed(r.json())]
    등락 = None
    try:
        t = httpx.get(f"{_업비트}/ticker", params={"markets": 마켓}, headers=머리, timeout=6)
        t.raise_for_status()
        등락 = float(t.json()[0]["signed_change_rate"])
    except Exception:
        pass
    return 봉들, 등락


# ---------- 그리기 ----------
_글꼴폴더 = "/usr/share/fonts/truetype/nanum/"
_글꼴캐시 = {}


def _글꼴(크기, 굵게=False):
    키 = (크기, 굵게)
    if 키 not in _글꼴캐시:
        후보 = ("NanumSquareB.ttf", "NanumGothicBold.ttf") if 굵게 else ("NanumSquareR.ttf", "NanumGothic.ttf")
        for 이름 in 후보:
            try:
                _글꼴캐시[키] = ImageFont.truetype(_글꼴폴더 + 이름, 크기)
                break
            except OSError:
                continue
        else:
            _글꼴캐시[키] = ImageFont.load_default(크기)
    return _글꼴캐시[키]


_대체글꼴 = ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",          # 한자
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf")      # 그 밖의 글자


def _이름조각(글, 크기):
    """[(글 조각, 글꼴)] — 나눔스퀘어에 없는 글자(한자 등)는 다른 글꼴로 그린다"""
    글꼴들 = [_글꼴(크기, True)]
    for 경로 in _대체글꼴:
        키 = (크기, 경로)
        if 키 not in _글꼴캐시:
            try:
                _글꼴캐시[키] = ImageFont.truetype(경로, 크기)
            except OSError:
                _글꼴캐시[키] = None
        if _글꼴캐시[키]:
            글꼴들.append(_글꼴캐시[키])
    조각 = []
    for 글자 in 글:
        고른 = 글꼴들[0]
        if not 글자.isspace():
            고른 = next((f for f in 글꼴들 if f.getmask(글자).getbbox()), 글꼴들[-1])
        if 조각 and 조각[-1][1] is 고른:
            조각[-1][0] += 글자
        else:
            조각.append([글자, 고른])
    return 조각


바탕 = "#17191E"
격자 = "#2A2D35"
글_진한 = "#F2F3F5"
글_보통 = "#A3A9B5"
글_흐린 = "#6E7584"
오름 = "#F04452"
내림 = "#4C8DFF"

너비, 높이 = 1200, 700
_왼, _오, _위, _아래 = 40, 1070, 180, 612        # 그래프 영역


def _눈금(낮, 높, 개수=5):
    """보기 좋은 가격 눈금들"""
    폭 = max(높 - 낮, 1)
    거친 = 폭 / 개수
    자리 = 10 ** math.floor(math.log10(거친))
    간격 = next(m * 자리 for m in (1, 2, 2.5, 5, 10) if m * 자리 >= 거친)
    간격 = max(1, 간격)
    v = math.ceil(낮 / 간격) * 간격
    결과 = []
    while v <= 높:
        결과.append(v)
        v += 간격
    return 결과


def _시각표시(봉들, 구간):
    """[(봉 번호, 글, 우선)] 가로축에 적을 후보"""
    후보 = []
    앞날 = None
    for i, 봉 in enumerate(봉들):
        t = datetime.fromisoformat(봉[0])
        새날 = 앞날 is not None and t.date() != 앞날
        날짜글 = f"{t.month}/{t.day}"
        if 구간 == "all":
            if 앞날 is None or t.month != 앞달:
                후보.append((i, f"{t.year % 100}.{t.month}" if t.month == 1 or 앞날 is None else f"{t.month}월", True))
            else:
                후보.append((i, 날짜글, False))
        elif 구간 == "1w":
            if 새날 or 앞날 is None:
                후보.append((i, 날짜글, True))
        elif 구간 == "1d":
            if 새날:
                후보.append((i, 날짜글, True))
            elif t.minute == 0:
                후보.append((i, f"{t.hour:02d}:00", False))
        else:
            if t.minute % 10 == 0:
                후보.append((i, f"{t.hour:02d}:{t.minute:02d}", t.minute == 0))
        앞날, 앞달 = t.date(), t.month
    return 후보


def 그리기(봉들, 이름, 심볼, 시장, 현재가, 등락, 구간, 상태글="", 코인=False):
    """
    봉들: [(시각 ISO, 시, 고, 저, 종[, 거래량])] 오래된 것부터. 등락: 전일 대비 비율 또는 None.
    거래량이 있으면 아래쪽에 거래량 막대를 같이 그린다.
    돌려주는 값: PNG bytes. 봉이 없으면 None
    """
    봉들 = list(봉들)[-최대봉:]
    if not 봉들:
        return None
    n = len(봉들)
    그림 = Image.new("RGB", (너비, 높이), 바탕)
    d = ImageDraw.Draw(그림)

    # ----- 머리 -----
    f이름, f작은, f가격, f등락 = _글꼴(30, True), _글꼴(20), _글꼴(54, True), _글꼴(24, True)
    이름폭 = lambda 글: sum(d.textlength(t, font=f) for t, f in _이름조각(글, 30))      # noqa: E731
    이름글 = 이름
    while 이름폭(이름글) > 620 and len(이름글) > 2:
        이름글 = 이름글[:-2] + "…"
    x = _왼
    for 글, 글꼴 in _이름조각(이름글, 30):
        d.text((x, 62), 글, font=글꼴, fill=글_진한, anchor="ls")
        x += d.textlength(글, font=글꼴)
    d.text((x + 14, 62), f"{심볼} · {시장}", font=f작은, fill=글_보통, anchor="ls")

    가격글 = f"{현재가:,}"
    d.text((_왼, 132), 가격글, font=f가격, fill=글_진한, anchor="ls")
    x = _왼 + d.textlength(가격글, font=f가격) + 6
    d.text((x, 132), "원", font=_글꼴(26, True), fill=글_보통, anchor="ls")
    x += 44
    if 등락 is not None:
        색 = 오름 if 등락 > 0 else 내림 if 등락 < 0 else 글_보통
        if 등락 > 0:
            d.polygon([(x, 130), (x + 18, 130), (x + 9, 112)], fill=색)
            x += 26
        elif 등락 < 0:
            d.polygon([(x, 113), (x + 18, 113), (x + 9, 131)], fill=색)
            x += 26
        글 = f"{등락 * 100:+,.2f}%"
        d.text((x, 132), 글, font=f등락, fill=색, anchor="ls")
        d.text((x + d.textlength(글, font=f등락) + 10, 132), "전일 대비", font=f작은, fill=글_보통, anchor="ls")

    기간, 주식봉, 코인봉 = 구간이름.get(구간, 구간이름["1d"])
    오른쪽 = 너비 - 40
    d.text((오른쪽, 62), f"{기간} · {코인봉 if 코인 else 주식봉}", font=_글꼴(22, True), fill=글_진한, anchor="rs")
    if 상태글:
        d.text((오른쪽, 94), 상태글, font=_글꼴(18), fill=글_보통, anchor="rs")
    최고 = max(b[2] for b in 봉들)
    최저 = min(b[3] for b in 봉들)
    처음, 마지막 = 봉들[0][1], 봉들[-1][4]
    if 처음:
        d.text((오른쪽, 132), f"이 기간 {(마지막 - 처음) / 처음 * 100:+,.2f}%", font=_글꼴(18), fill=글_보통, anchor="rs")

    # ----- 세로축 -----
    거래량 = [float(b[5] or 0) if len(b) > 5 else 0.0 for b in 봉들]
    최대량 = max(거래량) if 거래량 else 0
    가격아래 = _아래 - 92 if 최대량 > 0 else _아래        # 거래량이 있으면 아래 92px 를 거래량 칸으로
    여유 = max((최고 - 최저) * 0.09, 최고 * 0.0004, 1)
    낮, 높 = 최저 - 여유, 최고 + 여유
    y좌표 = lambda v: 가격아래 - (v - 낮) / (높 - 낮) * (가격아래 - _위)       # noqa: E731
    f축 = _글꼴(17)
    현재y = round(y좌표(봉들[-1][4]))
    for v in _눈금(낮, 높):
        y = round(y좌표(v))
        d.line([(_왼, y), (_오, y)], fill=격자, width=1)
        if abs(y - 현재y) > 26:                          # 현재가 딱지와 겹치면 숫자는 뺀다
            d.text((_오 + 12, y), f"{int(v):,}", font=f축, fill=글_흐린, anchor="lm")

    # ----- 봉 -----
    칸 = (_오 - _왼) / max(n, 40)
    몸폭 = max(1, min(18, int(칸 * 0.68)))
    심지 = 2 if 몸폭 >= 8 else 1
    중심 = lambda i: _오 - (n - i - 0.5) * 칸                              # noqa: E731  (오른쪽에 붙인다)
    if 최대량 > 0:
        d.line([(_왼, 가격아래 + 10), (_오, 가격아래 + 10)], fill=격자, width=1)
        d.text((_왼 + 2, 가격아래 + 16), f"거래량 · 최대 {round(최대량):,}주", font=_글꼴(14), fill=글_흐린, anchor="lt")
        연오름, 연내림, 연보통 = "#F8A2A9", "#A6C6FF", "#C9CDD4"
        for i, b in enumerate(봉들):
            if 거래량[i] <= 0:
                continue
            o, c = b[1], b[4]
            색 = 연오름 if c > o else 연내림 if c < o else 연보통
            cx = 중심(i)
            x0 = round(cx - 몸폭 / 2)
            위 = round(_아래 - 거래량[i] / 최대량 * 62)
            d.rectangle([x0, min(위, _아래 - 1), x0 + 몸폭 - 1, _아래], fill=색)
    for i, b in enumerate(봉들):
        o, h, l, c = b[1:5]
        색 = 오름 if c > o else 내림 if c < o else 글_보통
        cx = 중심(i)
        x0 = round(cx - 몸폭 / 2)
        wx = round(cx - 심지 / 2)
        d.rectangle([wx, round(y좌표(h)), wx + 심지 - 1, round(y좌표(l))], fill=색)
        위, 아래 = sorted((round(y좌표(o)), round(y좌표(c))))
        d.rectangle([x0, 위, x0 + 몸폭 - 1, max(아래, 위 + 1)], fill=색)

    # ----- 가로축 -----
    찍힌 = []
    후보 = _시각표시(봉들, 구간)
    for 우선 in (True, False):
        for i, 글, 중요 in 후보:
            if 중요 != 우선:
                continue
            cx = 중심(i)
            반 = d.textlength(글, font=f축) / 2
            if cx - 반 < _왼 or cx + 반 > _오:
                continue
            if any(abs(cx - 다른) < 95 for 다른 in 찍힌):
                continue
            찍힌.append(cx)
            d.text((cx, _아래 + 24), 글, font=f축, fill=글_보통 if 중요 else 글_흐린, anchor="mm")
    d.line([(_왼, _아래), (_오, _아래)], fill=격자, width=1)

    # ----- 최고·최저 표시 -----
    f표 = _글꼴(16)
    for 값, 위치, 위쪽 in ((최고, 2, True), (최저, 3, False)):
        i = next(k for k, b in enumerate(봉들) if b[위치] == 값)
        글 = f"{'최고' if 위쪽 else '최저'} {값:,}"
        폭 = d.textlength(글, font=f표)
        cx = min(max(중심(i), _왼 + 폭 / 2), _오 - 폭 / 2)
        d.text((cx, y좌표(값) + (-14 if 위쪽 else 14)), 글, font=f표, fill=글_보통, anchor="mm")

    # ----- 현재가 선 -----
    끝 = 봉들[-1]
    색 = 오름 if 끝[4] > 끝[1] else 내림 if 끝[4] < 끝[1] else "#5B6270"
    y = round(y좌표(끝[4]))
    for x in range(_왼, _오, 8):
        d.line([(x, y), (x + 3, y)], fill=색, width=1)
    글 = f"{끝[4]:,}"
    폭 = d.textlength(글, font=_글꼴(17, True))
    d.rounded_rectangle([_오 + 6, y - 14, _오 + 6 + 폭 + 14, y + 14], radius=5, fill=색)
    d.text((_오 + 13, y), 글, font=_글꼴(17, True), fill="#FFFFFF", anchor="lm")

    d.text((_왼, 높이 - 20), f"우리거래소 · {datetime.now():%Y-%m-%d %H:%M} 기준", font=_글꼴(15), fill=글_흐린, anchor="ls")

    통 = io.BytesIO()
    그림.save(통, format="PNG", optimize=True)
    return 통.getvalue()


# ============================================================
# 4. 레버리지 (코스피·코스닥, 롱/인버스)
# ============================================================
# 주식 레버리지 (코스피·코스닥만). 봇·웹이 같이 쓴다.
#
# 증거금을 걸고 2·4·8·16배로 오르는 쪽(롱) 또는 내리는 쪽(인버스)에 건다.
#   평가액 = 증거금 × (1 + 배율 × 등락률)        (인버스는 등락률 부호를 뒤집는다)
#   등락률 = 엔진 기준가(stocks.base_price) 기준. 매매로 생기는 수급 충격은 빼고 시장 움직임만 따라간다.
#          (현재가를 쓰면 주식을 잔뜩 샀다 팔아서 레버리지 손익을 조작할 수 있다)
# 평가액이 0 이 되면 강제 청산 (증거금 전부 잃음, 빚은 안 생김). 봇의 주가변동 루프가 틱마다 검사한다.
#   자동 충전(열 때 고름)을 켠 포지션은 청산 대신 처음 증거금만큼 다시 채우고 지금 기준가에서 다시 시작한다.
#   현금에서 먼저, 모자라면 대출(기존 대출 규칙: 합계 원금 tools.최대_대출금까지, 차단된 사람 안 됨)로 채운다.
#   둘 다 안 되면 그때 강제 청산. 넣은 돈은 added, 충전 횟수는 refills 에 쌓인다.
# 열고 닫는 건 주식 장중에만. 장이 닫혀 있는 동안 들고 있으면 다음 개장 갭을 그대로 맞는다.
#
# 돈은 tools.돈변경 으로만 바꾼다. 닫기는 포지션 DELETE 가 성공한 쪽만 돈을 받는다 (봇·웹 동시 청산 방지).

# tools 는 여기서 불러온다. 위 칸들은 함수 안에서만 tools 를 쓰고, 아래 칸은 처음부터 tools.cursor 를 쓴다.
# tools.py 가 먼저 불리면(봇·웹 모두) tools 가 반쯤 읽힌 채 여기로 오는데, conn·cursor 는 그 전에 만들어져 있다.
import tools

배율들 = (2, 3, 4)
수수료 = 0.001             # 포지션 크기(증거금 × 배율)의 0.1%. 열 때 한 번, 닫을 때 한 번
최소증거금 = 10_000
최대증거금 = 100_000_000   # 포지션 하나에 처음 넣을 수 있는 증거금 (자동 충전으로 다시 채우는 건 상관없음)
최대포지션 = 10            # 한 사람이 동시에 들고 있을 수 있는 포지션 수
방향이름 = {"long": "롱", "short": "인버스"}

cursor, conn = tools.cursor, tools.conn

cursor.execute("""
    CREATE TABLE IF NOT EXISTS leverage_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, lev INTEGER NOT NULL,
        margin INTEGER NOT NULL, entry REAL NOT NULL, entry_price INTEGER, opened_at TEXT, channel_id TEXT
    )
""")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_leverage_user ON leverage_positions (user_id)")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS leverage_history (
        id INTEGER, user_id TEXT, symbol TEXT, name TEXT, side TEXT, lev INTEGER, margin INTEGER,
        entry REAL, exit REAL, payout INTEGER, reason TEXT, opened_at TEXT, closed_at TEXT
    )
""")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_leverage_history_user ON leverage_history (user_id, closed_at)")
for 표, 열들 in (("leverage_positions", ("auto_refill INTEGER DEFAULT 0", "added INTEGER DEFAULT 0", "refills INTEGER DEFAULT 0")),
                ("leverage_history", ("refills INTEGER DEFAULT 0",))):
    있는것 = {r[1] for r in cursor.execute(f"PRAGMA table_info({표})")}
    for 열 in 열들:
        if 열.split()[0] not in 있는것:
            try:
                cursor.execute(f"ALTER TABLE {표} ADD COLUMN {열}")
            except Exception as e:                # 봇·웹이 동시에 켜지며 다른 쪽이 먼저 추가했다
                if "duplicate column" not in str(e):
                    raise
conn.commit()


# ---------- 계산 ----------
def 등락(포지션, 기준가):
    """진입 후 기준가 등락률 (인버스면 부호를 뒤집은 값)"""
    r = 기준가 / 포지션["entry"] - 1
    return -r if 포지션["side"] == "short" else r


def 원평가(포지션, 기준가):
    """지금 가치 (수수료 빼기 전). 갭·VI 해제로 한 번에 0 밑까지 가면 음수 그대로"""
    return int(포지션["margin"] * (1 + 포지션["lev"] * 등락(포지션, 기준가)))


def 평가액(포지션, 기준가):
    """지금 가치 (수수료 빼기 전). 0 아래로는 안 내려간다"""
    return max(0, 원평가(포지션, 기준가))


def 빚(포지션, 기준가):
    """0 밑으로 떨어진 만큼 = 강제 청산 때 갚아야 하는 돈"""
    return max(0, -원평가(포지션, 기준가))


def 닫기수수료(포지션):
    return int(포지션["margin"] * 포지션["lev"] * 수수료)


def 받을돈(포지션, 기준가):
    """지금 닫으면 받는 돈"""
    return max(0, 평가액(포지션, 기준가) - 닫기수수료(포지션))


def 청산까지(포지션, 기준가):
    """기준가가 지금에서 몇 % 더 움직이면 강제 청산인지 (롱은 음수, 인버스는 양수)"""
    부호 = -1 if 포지션["side"] == "long" else 1
    청산가 = 포지션["entry"] * (1 + 부호 / 포지션["lev"])
    return 청산가 / 기준가 - 1


def 금액해석(글):
    """'100만', '1억 5000만', '1,000,000', '50000' → 정수. 못 읽으면 None"""
    글 = str(글).replace(",", "").replace(" ", "").replace("원", "")
    if not 글:
        return None
    if 글.isdigit():
        return int(글)
    m = re.fullmatch(r"(?:(\d+)억)?(?:(\d+)만)?(\d+)?", 글)
    if not m or not any(m.groups()):
        return None
    억, 만, 나머지 = (int(x) if x else 0 for x in m.groups())
    return 억 * 100_000_000 + 만 * 10_000 + 나머지


# ---------- 조회 ----------
_레버열 = "id, user_id, symbol, side, lev, margin, entry, entry_price, opened_at, channel_id, auto_refill, added, refills"
_열수 = len(_레버열.split(", "))


def _행(row):
    p = dict(zip(_레버열.split(", "), row))
    p["auto_refill"] = bool(p.get("auto_refill"))
    p["added"] = int(p.get("added") or 0)
    p["refills"] = int(p.get("refills") or 0)
    p["invested"] = p["margin"] + p["added"]          # 처음 증거금 + 자동 충전으로 넣은 돈
    return p


def _레버종목(cur, 종목):
    """(symbol, name, market, 기준가, 현재가) 또는 None"""
    cur.execute(
        "SELECT symbol, name, is_premium, base_price, price FROM stocks WHERE symbol = ? OR name = ?",
        (str(종목).upper().strip(), str(종목).strip()),
    )
    r = cur.fetchone()
    if r is None:
        return None
    기준 = float(r[3] or 0) or float(r[4] or 0)
    return r[0], r[1], r[2], 기준, int(float(r[4] or 0))


def 목록(user_id):
    """내 포지션들. 각 dict 에 name, now(기준가), price(현재가), value(받을 돈), rate(손익률), liq(청산까지) 를 붙인다"""
    cursor.execute(
        f"SELECT {_레버열} FROM leverage_positions WHERE user_id = ? ORDER BY id", (str(user_id),))
    결과 = []
    for row in cursor.fetchall():
        p = _행(row)
        s = _레버종목(cursor, p["symbol"])
        if s is None:
            continue
        _, p["name"], _, 기준, p["price"] = s
        p["now"] = 기준
        p["move"] = 기준 / p["entry"] - 1
        p["value"] = 받을돈(p, 기준)
        p["profit"] = p["value"] - p["invested"]
        p["rate"] = p["profit"] / p["invested"] if p["invested"] else 0
        p["liq"] = 청산까지(p, 기준)
        결과.append(p)
    return 결과


def 평가합(user_id):
    """총자산용: 지금 다 닫으면 받는 돈"""
    return sum(p["value"] for p in 목록(user_id))


def 기록(user_id, 개수=10):
    cursor.execute(
        "SELECT id, symbol, name, side, lev, margin, payout, reason, closed_at, refills FROM leverage_history "
        "WHERE user_id = ? ORDER BY closed_at DESC LIMIT ?", (str(user_id), 개수))
    키 = ("id", "symbol", "name", "side", "lev", "margin", "payout", "reason", "closed_at", "refills")
    return [dict(zip(키, r)) for r in cursor.fetchall()]


# ---------- 열기 / 닫기 ----------
def 열기(user_id, 종목, 방향, 배율, 증거금, channel_id=None, 자동충전=False):
    """
    반환: (성공, 결과 dict 또는 오류 문자열)
    방향: "long" / "short". 증거금: 숫자·'100만'·'전부'
    자동충전: 평가액이 0 이 되면 청산 대신 현금 → 대출 순으로 처음 증거금만큼 다시 채운다
    """
    user_id = str(user_id)
    if 방향 not in 방향이름:
        return False, "방향은 롱(매수) 또는 인버스예요."
    try:
        배율 = int(str(배율).lower().rstrip("x배"))
    except ValueError:
        return False, f"배율은 {'·'.join(map(str, 배율들))}배 중 하나예요."
    if 배율 not in 배율들:
        return False, f"배율은 {'·'.join(map(str, 배율들))}배 중 하나예요."
    s = _레버종목(cursor, 종목 or "")
    if s is None:
        return False, f"'{종목}'은(는) 존재하지 않는 종목입니다."
    심볼, 이름, 시장, 기준, 현재가 = s
    if 시장 not in (0, 1):
        return False, "레버리지는 코스피·코스닥 주식만 돼요."
    cursor.execute("SELECT 1 FROM stock_etfs WHERE symbol = ? AND removed = 0", (심볼,))
    if cursor.fetchone():
        return False, "ETF 는 레버리지를 걸 수 없어요. 레버리지·인버스 ETF(예: KOSPI2X, KOSPIINV)를 그냥 사면 돼요."
    막힘 = 거래불가() or 거래정지(cursor, 심볼)
    if 막힘:
        return False, 막힘
    if 기준 <= 0:
        return False, "이 종목은 아직 가격이 정해지지 않았어요."

    cursor.execute("SELECT COUNT(*) FROM leverage_positions WHERE user_id = ?", (user_id,))
    if cursor.fetchone()[0] >= 최대포지션:
        return False, f"레버리지는 한 사람이 {최대포지션}개까지만 열 수 있어요. `!레버리지 청산 [번호]` 로 하나 닫아주세요."

    현금 = tools._get_money(user_id)
    if 현금 is None:
        return False, "아직 등록되지 않은 유저입니다. !출석 을 먼저 사용해주세요."
    비율 = 1 + 배율 * 수수료                      # 증거금 1원당 실제로 빠지는 돈
    if str(증거금).strip() in tools._전부:
        금액 = min(int(현금 / 비율), 최대증거금)
    else:
        금액 = 금액해석(증거금)
        if 금액 is None:
            return False, "증거금은 숫자(예: 100000, 100만)나 '전부'로 적어주세요."
    if 금액 < 최소증거금:
        return False, f"증거금은 {최소증거금:,}원 이상이어야 해요."
    if 금액 > 최대증거금:
        return False, f"증거금은 포지션 하나에 {최대증거금:,}원까지예요."
    열기비 = int(금액 * 배율 * 수수료)
    총액 = 금액 + 열기비
    if not tools.돈변경(user_id, -총액, 커밋=False):          # 차감을 맨 먼저 (실패하면 돈변경이 롤백한다)
        return False, f"잔액이 부족합니다. 필요 {총액:,}원 (증거금 {금액:,} + 수수료 {열기비:,}) / 보유 {현금:,}원"
    지금 = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO leverage_positions (user_id, symbol, side, lev, margin, entry, entry_price, opened_at, channel_id, auto_refill) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, 심볼, 방향, 배율, 금액, 기준, 현재가, 지금, str(channel_id) if channel_id else None, 1 if 자동충전 else 0),
    )
    번호 = cursor.lastrowid
    conn.commit()
    p = {"id": 번호, "symbol": 심볼, "side": 방향, "lev": 배율, "margin": 금액, "entry": 기준}
    return True, {
        "id": 번호, "name": 이름, "symbol": 심볼, "side": 방향, "lev": 배율, "margin": 금액, "fee": 열기비,
        "cost": 총액, "price": 현재가, "liq": 청산까지(p, 기준), "cash": 현금 - 총액, "auto_refill": bool(자동충전),
    }


def _닫기(cur, p, 기준, 이유, 지급=True, 갚은빚=0):
    """포지션 하나를 지우고 기록을 남긴다. 다른 쪽이 먼저 지웠으면 None. 커밋은 부른 쪽에서.
    기록의 margin 은 넣은 돈 전부(처음 증거금 + 자동 충전), payout 은 받은 돈 − 갚은 빚(음수면 빚까지 낸 것)."""
    cur.execute("DELETE FROM leverage_positions WHERE id = ?", (p["id"],))
    if cur.rowcount == 0:
        return None
    돈 = 받을돈(p, 기준) if 지급 else 0
    cur.execute(
        "INSERT INTO leverage_history (id, user_id, symbol, name, side, lev, margin, entry, exit, payout, reason, opened_at, closed_at, refills) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p["id"], p["user_id"], p["symbol"], p.get("name"), p["side"], p["lev"], p["invested"], p["entry"], 기준,
         돈 - 갚은빚, 이유, p["opened_at"], datetime.now().isoformat(timespec="seconds"), p["refills"]),
    )
    return 돈


def 닫기(user_id, 번호):
    """
    번호: 포지션 번호 또는 '전부'
    반환: (성공, {"closed": [{id, name, symbol, side, lev, margin, payout, profit}], "total", "cash"} 또는 오류)
    """
    user_id = str(user_id)
    막힘 = 거래불가()
    if 막힘:
        return False, 막힘
    내것 = 목록(user_id)
    if not 내것:
        return False, "열려 있는 레버리지가 없어요."
    if str(번호).strip() in tools._전부:
        고른 = 내것
    else:
        try:
            n = int(str(번호).lstrip("#"))
        except ValueError:
            return False, "청산할 번호를 적어주세요. 예) `!레버리지 청산 3` · `!레버리지 청산 전부`"
        고른 = [p for p in 내것 if p["id"] == n]
        if not 고른:
            return False, f"#{n} 은(는) 내 레버리지가 아니에요. `!레버리지` 로 번호를 확인해주세요."
    정지 = 정지목록(cursor)
    멈춘 = [p for p in 고른 if p["symbol"] in 정지]
    고른 = [p for p in 고른 if p["symbol"] not in 정지]
    if not 고른:
        return False, 정지[멈춘[0]["symbol"]]["글"] + " (레버리지도 그동안 닫을 수 없어요)"
    닫힌 = []
    합계 = 0
    for p in 고른:
        돈 = _닫기(cursor, p, p["now"], "청산")
        if 돈 is None:
            continue              # 그 사이 강제 청산됐다
        합계 += 돈
        닫힌.append({"id": p["id"], "name": p["name"], "symbol": p["symbol"], "side": p["side"], "lev": p["lev"],
                    "margin": p["invested"], "payout": 돈, "profit": 돈 - p["invested"], "move": p["move"],
                    "refills": p["refills"]})
    if not 닫힌:
        conn.rollback()
        return False, "그 사이 강제 청산됐어요. `!레버리지 기록` 으로 확인해주세요."
    if 합계:
        tools.돈변경(user_id, 합계, 커밋=False)
    conn.commit()
    return True, {"closed": 닫힌, "total": 합계, "cash": tools._get_money(user_id) or 0}


# ---------- 봇 루프·관리용 ----------
def 강제청산(con, cur):
    """
    평가액이 0 이 된 포지션을 처리한다. 봇의 주가변동 루프가 틱마다 (엔진 전용 연결로) 부른다.
    자동 충전이 꺼진 건 엔진 연결로 지우고 커밋한다 (돈이 오가지 않는다).
    켜진 건 엔진 연결을 커밋한 뒤 _자동충전 이 tools 연결(돈변경)로 처리한다. 같은 스레드에서 두 연결이
    동시에 쓰기 잠금을 잡으면 서로 기다리다 멈추니 순서를 지킨다.
    반환: 알림용 [포지션 dict + kind("청산"/"충전"), 충전이면 cash·loan, 청산인데 충전 실패면 fail]
    """
    cur.execute(
        f"SELECT {', '.join('p.' + x for x in _레버열.split(', '))}, s.name, s.base_price, s.price "
        "FROM leverage_positions p JOIN stocks s ON s.symbol = p.symbol")
    대상 = []
    for row in cur.fetchall():
        p = _행(row[:_열수])
        p["name"] = row[_열수]
        기준 = float(row[_열수 + 1] or 0) or float(row[_열수 + 2] or 0)
        현재가 = int(float(row[_열수 + 2] or 0))
        if 기준 > 0 and 평가액(p, 기준) <= 0:
            대상.append((p, 기준, 현재가))
    if not 대상:
        return []
    알림 = []
    for p, 기준, _ in 대상:              # 돈이 안 오가는 것(자동 충전 없음 + 0 밑으로 안 감)만 엔진 연결로
        if not p["auto_refill"] and 빚(p, 기준) == 0 and _닫기(cur, p, 기준, "강제청산", 지급=False) is not None:
            알림.append({**p, "kind": "청산"})
    con.commit()
    for p, 기준, 현재가 in 대상:
        if p["auto_refill"] or 빚(p, 기준) > 0:
            try:
                결과 = _자동충전(p, 기준, 현재가) if p["auto_refill"] else _빚청산(p, 기준)
            except Exception as e:                # tools 연결을 잠근 채로 두면 봇 전체가 멈춘다
                conn.rollback()
                print(f"[{datetime.now()}] 레버리지 강제청산 처리 실패 #{p['id']}: {type(e).__name__}: {e}")
                continue
            if 결과 is not None:
                알림.append(결과)
    return 알림


def _빚내기(user_id, 금액):
    """강제 청산 때 0 밑으로 떨어진 만큼을 갚게 한다. 현금에서 먼저, 모자라면 대출 원금에 더한다
    (빚이라 대출 한도·차단과 상관없이 붙는다). tools 연결, 커밋은 안 한다. 반환: (현금분, 대출분)"""
    현금 = max(0, tools._get_money(user_id) or 0)
    현금분 = min(현금, 금액)
    if 현금분 and not tools.돈변경(user_id, -현금분, 커밋=False):    # 그 사이 줄었다 (돈변경이 롤백) → 전부 대출로
        현금분 = 0
    대출분 = 금액 - 현금분
    if 대출분:
        cursor.execute("UPDATE loans SET principal = CAST(CAST(principal AS INTEGER) + ? AS TEXT) WHERE user_id = ?",
                       (대출분, user_id))
        if cursor.rowcount == 0:
            지금 = datetime.now()
            cursor.execute(
                "INSERT INTO loans (user_id, principal, interest, borrowed_at, due_at) VALUES (?, ?, '0', ?, ?)",
                (user_id, str(대출분), 지금.isoformat(), (지금 + timedelta(days=tools.대출_기한_일)).isoformat()))
    return 현금분, 대출분


def _빚청산(p, 기준):
    """자동 충전이 꺼진 포지션이 0 밑까지 떨어졌다. 모자란 만큼 갚게 하고 강제 청산. 반환: 알림 dict 또는 None(그 사이 닫힘)"""
    d = 빚(p, 기준)
    현금분, 대출분 = _빚내기(p["user_id"], d)
    if _닫기(cursor, p, 기준, "강제청산", 지급=False, 갚은빚=d) is None:
        conn.rollback()
        return None
    conn.commit()
    return {**p, "kind": "청산", "debt": d, "debt_cash": 현금분, "debt_loan": 대출분}


def _대출로(user_id, 금액):
    """자동 충전용 대출. 기존 대출이 있으면 원금에 더한다 (합계 원금 tools.최대_대출금까지). 커밋은 안 한다.
    반환: None(성공) 또는 실패 사유"""
    사유 = tools._대출_차단여부(user_id)
    if 사유:
        return f"대출 차단 상태 ({사유})"
    한도 = tools.최대_대출금
    cursor.execute(
        "UPDATE loans SET principal = CAST(CAST(principal AS INTEGER) + ? AS TEXT) "
        "WHERE user_id = ? AND CAST(principal AS INTEGER) + ? <= ?", (금액, user_id, 금액, 한도))
    if cursor.rowcount:
        return None
    cursor.execute("SELECT principal FROM loans WHERE user_id = ?", (user_id,))
    r = cursor.fetchone()
    if r is not None:
        return f"대출 한도 초과 (지금 원금 {int(float(r[0])):,}원 + 필요 {금액:,}원 > {한도:,}원)"
    if 금액 > 한도:
        return f"대출 한도 초과 (필요 {금액:,}원 > {한도:,}원)"
    지금 = datetime.now()
    cursor.execute(
        "INSERT OR IGNORE INTO loans (user_id, principal, interest, borrowed_at, due_at) VALUES (?, ?, '0', ?, ?)",
        (user_id, str(금액), 지금.isoformat(), (지금 + timedelta(days=tools.대출_기한_일)).isoformat()))
    return None if cursor.rowcount else "대출을 만들지 못했어요"


def _자동충전(p, 기준, 현재가):
    """평가액 0 이 된 자동 충전 포지션 하나. 현금 → 대출로 처음 증거금만큼 채우고 지금 기준가에서 다시 시작한다.
    못 채우면 강제 청산. tools 연결로 한 트랜잭션에 처리한다. 반환: 알림 dict 또는 None(그 사이 닫힘)"""
    uid = p["user_id"]
    d = 빚(p, 기준)                       # 0 밑으로 떨어진 만큼도 같이 낸다
    필요 = p["margin"] + d
    현금 = tools._get_money(uid) or 0
    현금분 = min(현금, 필요)
    대출분 = 필요 - 현금분
    실패 = None
    if 현금분 and not tools.돈변경(uid, -현금분, 커밋=False):      # 차감을 맨 먼저 (실패하면 돈변경이 롤백)
        실패 = "현금이 그 사이 줄었어요"
    elif 대출분:
        실패 = _대출로(uid, 대출분)
        if 실패:
            conn.rollback()                                        # 현금 차감도 되돌린다
    if 실패 is None:
        cursor.execute(
            "UPDATE leverage_positions SET entry = ?, entry_price = ?, added = added + ?, refills = refills + 1 WHERE id = ?",
            (기준, 현재가, 필요, p["id"]))
        if cursor.rowcount == 0:                                   # 그 사이 유저가 닫았다
            conn.rollback()
            return None
        conn.commit()
        새 = {**p, "entry": 기준, "added": p["added"] + 필요, "refills": p["refills"] + 1}
        새["invested"] = 새["margin"] + 새["added"]
        return {**새, "kind": "충전", "cash": 현금분, "loan": 대출분, "debt": d, "liq": 청산까지(새, 기준)}
    if d:
        결과 = _빚청산(p, 기준)
        return {**결과, "fail": 실패} if 결과 else None
    if _닫기(cursor, p, 기준, "강제청산", 지급=False) is None:
        conn.rollback()
        return None
    conn.commit()
    return {**p, "kind": "청산", "fail": 실패}


def 종목정리(심볼):
    """상장폐지 전에 부른다. 그 종목 포지션을 지금 값으로 정산해 돌려준다. 반환: [(user_id, 받은 돈)]"""
    cursor.execute(f"SELECT {_레버열} FROM leverage_positions WHERE symbol = ?", (심볼,))
    행들 = [_행(r) for r in cursor.fetchall()]
    s = _레버종목(cursor, 심볼)
    if not 행들 or s is None:
        return []
    기준 = s[3]
    결과 = []
    for p in 행들:
        p["name"] = s[1]
        돈 = _닫기(cursor, p, 기준, "상장폐지")
        if 돈 is None:
            continue
        if 돈:
            tools.돈변경(p["user_id"], 돈, 커밋=False)
        결과.append((p["user_id"], 돈))
    conn.commit()
    return 결과


# ============================================================
# 5. 배당 + 예약 주문
# ============================================================
# 주식 제도 — 배당 / 예약 주문(지정가·손절). 봇·웹·AI 툴이 같이 쓴다.
#
#   배당 : 분기 말(배당월의 마지막 거래일) 장 마감 때 들고 있는 주식 수만큼 현금을 준다 (배당소득세를 떼고).
#          다음 날 시가는 배당만큼 낮게 시작한다 (배당락, _개장 이 처리).
#          종목마다 연 배당률이 있다 (stocks.dividend_yield, 비어 있으면 업종으로 정한 기본값).
#   주문 : 지정가 매수(그 가격 이하가 되면 산다) / 지정가 매도(그 가격 이상이 되면 판다) / 손절(그 가격 이하가 되면 판다).
#          봇의 주가변동 루프가 틱마다 주문체결() 을 부른다. 체결은 tools.매수실행·매도실행 그대로라
#          수수료·세금·수급 충격·VI 가 똑같이 적용된다. 지정가는 체결 평균가가 그 가격을 넘지 않는 만큼만 체결된다 (부분 체결).
#
# 돈은 tools.돈변경 으로만 바꾼다. 쓰기는 tools.conn (봇 엔진 연결과 동시에 쓰지 않는다 — 봇은 엔진 커밋 뒤에 부른다).

# ============================================================
# 설정
# ============================================================
배당월 = (3, 6, 9, 12)          # 분기 배당. 그 달 마지막 거래일 장 마감 때 들고 있으면 받는다

# 업종별 기본 연 배당률(실제 한국 업종 평균쯤)과 배당을 안 주는 종목 비율
업종_배당 = {
    "금융": (0.050, 0.0), "공공": (0.045, 0.0), "자동차": (0.040, 0.0), "광업": (0.035, 0.1),
    "운송": (0.025, 0.2), "건설": (0.025, 0.2), "유통": (0.025, 0.1), "화학": (0.025, 0.1),
    "식품": (0.022, 0.1), "수공업": (0.020, 0.2), "종합": (0.020, 0.2), "반도체": (0.018, 0.2),
    "전자": (0.015, 0.2), "중공업": (0.012, 0.3), "IT": (0.008, 0.4), "게임": (0.010, 0.4),
    "엔터": (0.008, 0.4), "배터리": (0.005, 0.5), "조선": (0.005, 0.5), "바이오": (0.003, 0.7),
}
코스닥_배당배율 = 0.6           # 코스닥은 배당이 더 적고, 안 주는 회사가 더 많다
코스닥_무배당_더함 = 0.15

주문_최대 = 20                  # 한 사람이 걸어 둘 수 있는 주문 수
주문_유효일 = 7                 # 이 날수가 지나면 주문이 저절로 취소된다
주문종류 = {"buy": "지정가 매수", "sell": "지정가 매도", "stop": "손절"}
_종류별칭 = {
    "매수": "buy", "사기": "buy", "buy": "buy", "지정가매수": "buy",
    "매도": "sell", "팔기": "sell", "sell": "sell", "지정가매도": "sell", "익절": "sell",
    "손절": "stop", "스탑": "stop", "stop": "stop", "손절매도": "stop",
}

# ============================================================
# 테이블
# ============================================================
for 문 in (
    "ALTER TABLE stocks ADD COLUMN dividend_yield REAL",             # 연 배당률 (0.03 = 3%). 비면 기본값
):
    try:
        cursor.execute(문)
    except Exception:
        pass
cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_dividend_days (
        date TEXT PRIMARY KEY,
        done_at TEXT,
        holders INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_dividend_pay (
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        user_id TEXT NOT NULL,
        qty INTEGER,
        gross INTEGER,
        tax INTEGER,
        PRIMARY KEY (date, symbol, user_id)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        kind TEXT NOT NULL,
        price INTEGER NOT NULL,
        qty INTEGER NOT NULL,
        filled INTEGER DEFAULT 0,
        amount INTEGER DEFAULT 0,
        status TEXT DEFAULT 'open',
        created TEXT,
        expires TEXT,
        done_at TEXT,
        note TEXT,
        channel_id TEXT
    )
""")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_orders_open ON stock_orders (status, user_id)")
conn.commit()


# ============================================================
# 배당
# ============================================================
def _해시(글):
    return int(hashlib.md5(글.encode()).hexdigest(), 16) / 16 ** 32


def 기본배당률(심볼, 업종, 시장):
    """업종·종목 코드로 정해지는 기본 연 배당률 (같은 종목은 늘 같다)"""
    평균, 무배당 = 업종_배당.get(업종, 업종_배당["종합"])
    if 시장 == 0:
        평균, 무배당 = 평균 * 코스닥_배당배율, 무배당 + 코스닥_무배당_더함
    if _해시(f"무배당|{심볼}") < 무배당:
        return 0.0
    return round(평균 * (0.4 + 1.2 * _해시(f"배당|{심볼}")), 4)


def _연율표():
    """{심볼: 연 배당률} — 코스피·코스닥 전 종목. ETF 는 구성 종목 평균 (1배만, 레버리지·인버스는 0)"""
    cursor.execute("SELECT symbol, name, is_premium, sector, dividend_yield FROM stocks WHERE is_premium IN (0, 1)")
    표 = {}
    for 심볼, 이름, 시장, 업종, 값 in cursor.fetchall():
        표[심볼] = float(값) if 값 is not None else 기본배당률(심볼, 종목업종(업종, 이름), 시장)
    표.update(분배율표(cursor, 표))
    return 표


def 배당률(심볼):
    """연 배당률 (ETF 는 분배율). 코인이나 없는 종목은 None"""
    cursor.execute("SELECT name, is_premium, sector, dividend_yield FROM stocks WHERE symbol = ?", (심볼,))
    행 = cursor.fetchone()
    if not 행 or 행[1] not in (0, 1):
        return None
    if 정의(cursor, 심볼):
        return _연율표().get(심볼, 0.0)
    이름, 시장, 업종, 값 = 행
    if 값 is not None:
        return float(값)
    return 기본배당률(심볼, 종목업종(업종, 이름), 시장)


def 배당률설정(심볼, 연율):
    """관리자용. 연율=None 이면 기본값으로"""
    cursor.execute(f"UPDATE stocks SET dividend_yield = ? WHERE symbol = ? AND is_premium IN (0, 1) AND {아님}",
                   (연율, 심볼))
    바뀜 = cursor.rowcount > 0
    if 바뀜:
        conn.commit()
    else:
        conn.rollback()
    return 바뀜


def 배당기준일(연, 월):
    """그 달의 마지막 거래일 (datetime 0시)"""
    다음달 = datetime(연 + (월 == 12), 월 % 12 + 1, 1)
    d = 다음달 - timedelta(days=1)
    while d.weekday() in 휴장요일:
        d -= timedelta(days=1)
    return d


def 다음배당일(지금=None):
    """오늘 포함 다음 배당 기준일 (datetime 0시). 오늘이 기준일인데 장이 이미 끝났으면 그다음"""
    지금 = 지금 or datetime.now()
    for 더 in range(0, 15):
        연, 월 = 지금.year + (지금.month - 1 + 더) // 12, (지금.month - 1 + 더) % 12 + 1
        if 월 not in 배당월:
            continue
        d = 배당기준일(연, 월)
        if 지금 < 장시간(d)[1]:
            return d
    return None


def 분기이름(d):
    return f"{d.year}년 {배당월.index(d.month) + 1 if d.month in 배당월 else '?'}분기"


def 배당처리(지금=None):
    """
    기준일 장이 끝났는데 아직 안 준 배당이 있으면 준다 (봇이 꺼져 있었어도 7일 안이면 나중에 준다).
    돌려주는 값: [{"date", "stocks": [(이름, 심볼, 주당, 받은 사람 수, 총액)], "users": {uid: (세전, 세금)}}]
    """
    지금 = 지금 or datetime.now()
    결과 = []
    for 뒤 in range(0, 8):
        날 = 지금 - timedelta(days=뒤)
        if 날.month not in 배당월:
            continue
        d = 배당기준일(날.year, 날.month)
        if d.date() != 날.date() or 지금 < 장시간(d)[1]:
            continue
        날짜 = f"{d:%Y-%m-%d}"
        cursor.execute("SELECT 1 FROM stock_dividend_days WHERE date = ?", (날짜,))
        if cursor.fetchone():
            continue
        결과.append(_배당주기(날짜))
    return 결과


def _배당주기(날짜):
    cursor.execute("INSERT INTO stock_dividend_days (date, done_at) VALUES (?, ?)",
                   (날짜, datetime.now().isoformat(timespec="seconds")))
    cursor.execute("SELECT symbol, name, is_premium, sector, dividend_yield, base_price, price FROM stocks "
                   "WHERE is_premium IN (0, 1)")
    종목들 = cursor.fetchall()
    연율표 = _연율표()
    회수 = len(배당월)
    목록, 사람 = [], {}
    for 심볼, 이름, 시장, 업종, 값, 기준, 가격 in 종목들:
        연율 = 연율표.get(심볼, 0.0)
        기준가 = float(기준 or 0) or float(가격 or 0)
        주당 = int(기준가 * 연율 / 회수)
        if 주당 < 1:
            continue
        cursor.execute("SELECT user_id, CAST(quantity AS INTEGER) FROM portfolio "
                       "WHERE symbol = ? AND CAST(quantity AS INTEGER) > 0", (심볼,))
        총액 = 받은수 = 0
        for uid, 수량 in cursor.fetchall():
            세전 = 수량 * 주당
            세금 = int(세전 * tools.배당세율)
            if not tools.돈변경(uid, 세전 - 세금, 커밋=False):
                continue
            tools.배당기록(uid, 세전, 세금)
            cursor.execute("INSERT OR REPLACE INTO stock_dividend_pay (date, symbol, user_id, qty, gross, tax) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (날짜, 심볼, uid, 수량, 세전, 세금))
            a, b = 사람.get(uid, (0, 0))
            사람[uid] = (a + 세전, b + 세금)
            총액 += 세전
            받은수 += 1
        cursor.execute("INSERT OR REPLACE INTO stock_dividends (date, symbol, dps, rate, holders, total, ex_done) "
                       "VALUES (?, ?, ?, ?, ?, ?, 0)", (날짜, 심볼, 주당, 주당 / 기준가 if 기준가 else 0, 받은수, 총액))
        목록.append((이름, 심볼, 주당, 받은수, 총액))
    cursor.execute("UPDATE stock_dividend_days SET holders = ?, total = ? WHERE date = ?",
                   (len(사람), sum(a for a, _ in 사람.values()), 날짜))
    conn.commit()
    print(f"[{datetime.now()}] 배당 지급 {날짜}: {len(목록)}종목, {len(사람)}명, 총 {sum(a for a, _ in 사람.values()):,}원")
    return {"date": 날짜, "stocks": 목록, "users": 사람}


def 배당표(지금=None):
    """[{symbol, name, market, sector, yield, dps(다음 기준일 예상 주당), price}] — 배당률 높은 순"""
    cursor.execute("SELECT symbol, name, is_premium, sector, dividend_yield, base_price, price FROM stocks "
                   "WHERE is_premium IN (0, 1)")
    행들 = cursor.fetchall()
    연율표 = _연율표()
    ETF들 = set(정의들(cursor))
    표 = []
    for 심볼, 이름, 시장, 업종, 값, 기준, 가격 in 행들:
        업 = "ETF" if 심볼 in ETF들 else 종목업종(업종, 이름)
        연율 = 연율표.get(심볼, 0.0)
        기준가 = float(기준 or 0) or float(가격 or 0)
        표.append({"symbol": 심볼, "name": 이름, "market": 시장, "sector": 업, "yield": 연율,
                  "dps": int(기준가 * 연율 / len(배당월)), "price": int(float(가격 or 0))})
    return sorted(표, key=lambda x: -x["yield"])


def 내배당(user_id, 개수=10):
    """최근 받은 배당 [(날짜, 이름, 심볼, 수량, 세전, 세금)]"""
    cursor.execute("SELECT p.date, COALESCE(s.name, p.symbol), p.symbol, p.qty, p.gross, p.tax "
                   "FROM stock_dividend_pay p LEFT JOIN stocks s ON s.symbol = p.symbol "
                   "WHERE p.user_id = ? ORDER BY p.date DESC, p.gross DESC LIMIT ?", (user_id, 개수))
    return cursor.fetchall()


def 예상배당(user_id):
    """지금 들고 있는 주식으로 다음 기준일에 받을 배당 (세전, 세금)"""
    표 = {x["symbol"]: x for x in 배당표()}
    cursor.execute("SELECT symbol, CAST(quantity AS INTEGER) FROM portfolio WHERE user_id = ? AND CAST(quantity AS INTEGER) > 0",
                   (user_id,))
    세전 = sum(q * 표[s]["dps"] for s, q in cursor.fetchall() if s in 표)
    return 세전, int(세전 * tools.배당세율)


# ============================================================
# 예약 주문
# ============================================================
def 종류해석(글):
    return _종류별칭.get(str(글 or "").strip().lower())


def _가격해석(글):
    try:
        값 = 금액해석(str(글))
    except Exception:
        값 = None
    if 값 is None:
        try:
            값 = int(float(str(글).replace(",", "")))
        except ValueError:
            return None
    return 값


def 주문넣기(user_id, 종류, 종목, 가격, 수량, channel_id=None):
    """반환: (성공, dict 또는 오류 문구)"""
    kind = 종류해석(종류) or (종류 if 종류 in 주문종류 else None)
    if kind is None:
        return False, "주문 종류는 `매수`(지정가 매수), `매도`(지정가 매도), `손절` 중 하나예요."
    정보 = tools.시장정보(str(종목 or ""))
    if 정보 is None:
        return False, f"'{종목}'은(는) 없는 종목입니다."
    if 정보["market"] == 2:
        return False, "코인은 예약 주문을 받지 않아요. 코스피·코스닥 종목만 돼요."
    값 = _가격해석(가격)
    if not 값 or 값 <= 0:
        return False, "가격은 1원 이상 숫자로 적어주세요. (예: `72000`, `7만2000`)"
    if str(수량).strip() in tools._전부:
        if kind == "buy":
            return False, "매수 주문은 수량을 숫자로 정해주세요."
        개수 = 0
    else:
        try:
            개수 = int(str(수량).replace(",", ""))
        except (TypeError, ValueError):
            return False, "수량은 숫자" + ("" if kind == "buy" else " 또는 `전부`") + "로 적어주세요."
        if 개수 <= 0:
            return False, "수량은 1 이상이어야 해요."
    if kind != "buy" and tools._내보유(user_id, 정보["symbol"]) <= 0:
        return False, f"{정보['name']}을(를) 갖고 있지 않아 팔 주문을 걸 수 없어요."
    if kind == "buy" and tools.추가매수가능(정보, user_id) == 0:
        return False, (f"{정보['name']}은(는) 더 살 수 있는 물량이 없어요 "
                       f"(1인 최대 {tools.지분한도(정보):,}주, 남은 물량 {정보['remain']:,}주).")
    cursor.execute("SELECT COUNT(*) FROM stock_orders WHERE user_id = ? AND status = 'open'", (user_id,))
    if cursor.fetchone()[0] >= 주문_최대:
        return False, f"주문은 한 사람이 {주문_최대}개까지 걸어 둘 수 있어요. `!주문 취소 [번호]` 로 정리해주세요."
    지금 = datetime.now()
    끝 = 지금 + timedelta(days=주문_유효일)
    cursor.execute(
        "INSERT INTO stock_orders (user_id, symbol, kind, price, qty, created, expires, channel_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, 정보["symbol"], kind, 값, 개수, 지금.isoformat(timespec="seconds"),
         끝.isoformat(timespec="seconds"), str(channel_id) if channel_id else None))
    번호 = cursor.lastrowid
    conn.commit()
    p = 정보["price"]
    바로 = (kind == "buy" and p <= 값) or (kind == "sell" and p >= 값) or (kind == "stop" and p <= 값)
    return True, {"id": 번호, "kind": kind, "kindName": 주문종류[kind], "symbol": 정보["symbol"], "name": 정보["name"],
                  "price": 값, "qty": 개수, "now": p, "immediate": 바로, "expires": 끝.isoformat(timespec="minutes")}


def 주문취소(user_id, 번호):
    """번호 = 주문 id 또는 '전부'. 반환: (성공, 취소한 개수 또는 오류 문구)"""
    if str(번호).strip() in tools._전부:
        cursor.execute("UPDATE stock_orders SET status = 'canceled', done_at = ?, note = '직접 취소' "
                       "WHERE user_id = ? AND status = 'open'", (datetime.now().isoformat(timespec="seconds"), user_id))
    else:
        try:
            i = int(str(번호).lstrip("#"))
        except ValueError:
            return False, "취소할 주문 번호를 적어주세요. (`!주문` 에서 확인)"
        cursor.execute("UPDATE stock_orders SET status = 'canceled', done_at = ?, note = '직접 취소' "
                       "WHERE id = ? AND user_id = ? AND status = 'open'",
                       (datetime.now().isoformat(timespec="seconds"), i, user_id))
    n = cursor.rowcount
    if n == 0:
        conn.rollback()
        return False, "취소할 주문이 없어요. (이미 체결됐거나 번호가 달라요)"
    conn.commit()
    return True, n


def _주문행(r):
    키 = ("id", "symbol", "name", "kind", "price", "qty", "filled", "amount", "status", "created", "expires", "done_at", "note")
    d = dict(zip(키, r))
    d["kindName"] = 주문종류.get(d["kind"], d["kind"])
    return d


def 주문목록(user_id, 열린것=True, 개수=15):
    조건 = "o.status = 'open'" if 열린것 else "o.status != 'open'"
    cursor.execute(
        "SELECT o.id, o.symbol, COALESCE(s.name, o.symbol), o.kind, o.price, o.qty, o.filled, o.amount, o.status, "
        "o.created, o.expires, o.done_at, o.note FROM stock_orders o LEFT JOIN stocks s ON s.symbol = o.symbol "
        f"WHERE o.user_id = ? AND {조건} ORDER BY o.id DESC LIMIT ?", (user_id, 개수))
    return [_주문행(r) for r in cursor.fetchall()]


def _최대체결(정보, 상한, 가격, 매수, 예산=None):
    """지정가 조건(평균 체결가가 가격을 넘지 않음/밑돌지 않음)과 예산 안에서 체결할 수 있는 최대 수량"""
    lo, hi = 0, max(0, 상한)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        평균, _, _ = tools._예상체결(정보, mid, 매수)
        된다 = 평균 <= 가격 if 매수 else 평균 >= 가격
        if 된다 and 예산 is not None:
            된다 = int(평균 * mid * (1 + tools.거래_수수료)) <= 예산
        if 된다:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _주문끝(o, 상태, 메모):
    cursor.execute("UPDATE stock_orders SET status = ?, done_at = ?, note = ? WHERE id = ? AND status = 'open'",
                   (상태, datetime.now().isoformat(timespec="seconds"), 메모, o["id"]))
    conn.commit()


def 주문체결(지금=None):
    """
    걸린 주문 중 조건이 맞은 것을 체결한다. 장중에만. 봇 주가변동 루프가 엔진 커밋 뒤에 부른다.
    돌려주는 값: 알림 [{"user_id", "channel_id", "text"}]
    """
    지금 = 지금 or datetime.now()
    if not 장중(지금):
        return []
    cursor.execute("SELECT id, user_id, symbol, kind, price, qty, filled, amount, expires, channel_id "
                   "FROM stock_orders WHERE status = 'open' ORDER BY id")
    키 = ("id", "user_id", "symbol", "kind", "price", "qty", "filled", "amount", "expires", "channel_id")
    주문들 = [dict(zip(키, r)) for r in cursor.fetchall()]
    if not 주문들:
        return []
    정지 = 정지목록(cursor, 지금)
    알림 = []

    def 알려(o, 글):
        알림.append({"user_id": o["user_id"], "channel_id": o["channel_id"], "text": 글})

    for o in 주문들:
        이름 = f"#{o['id']} {주문종류[o['kind']]}"
        if o["expires"] and 지금.isoformat(timespec="seconds") >= o["expires"]:
            _주문끝(o, "expired", "기간 만료")
            알려(o, f"⌛ 주문 {이름} {o['symbol']} {o['price']:,}원 — {주문_유효일}일이 지나 취소됐어요"
                   + (f" (체결 {o['filled']:,}주)" if o["filled"] else ""))
            continue
        if o["symbol"] in 정지:
            continue
        정보 = tools.시장정보(o["symbol"])
        if 정보 is None:
            _주문끝(o, "canceled", "종목 없음")
            알려(o, f"❌ 주문 {이름} {o['symbol']} — 종목이 없어져 취소됐어요")
            continue
        p = 정보["price"]
        if o["kind"] == "buy":
            if p > o["price"]:
                continue
            남은 = o["qty"] - o["filled"]
            가능 = tools.추가매수가능(정보, o["user_id"])
            if 가능 is not None:
                남은 = min(남은, 가능)
            예산 = tools._get_money(o["user_id"]) or 0
            k = _최대체결(정보, 남은, o["price"], True, 예산)
            if k <= 0:
                if 가능 == 0:
                    _주문끝(o, "failed", "지분 한도·남은 물량 없음")
                    알려(o, f"❌ 주문 {이름} {정보['name']} — 더 살 수 있는 물량이 없어 취소됐어요 (1인 지분 한도 또는 품절)")
                elif 예산 < p * (1 + tools.거래_수수료):
                    _주문끝(o, "failed", "잔액 부족")
                    알려(o, f"❌ 주문 {이름} {정보['name']} {o['price']:,}원 — 가격이 닿았는데 잔액이 모자라 취소됐어요")
                continue
            성공, r = tools.매수실행(o["user_id"], o["symbol"], k)
        else:
            if (o["kind"] == "sell" and p < o["price"]) or (o["kind"] == "stop" and p > o["price"]):
                continue
            보유 = tools._내보유(o["user_id"], o["symbol"])
            목표 = 보유 if o["qty"] == 0 else min(o["qty"] - o["filled"], 보유)
            if 목표 <= 0:
                _주문끝(o, "failed", "보유 없음")
                알려(o, f"❌ 주문 {이름} {정보['name']} — 팔 주식이 없어 취소됐어요")
                continue
            k = 목표 if o["kind"] == "stop" else _최대체결(정보, 목표, o["price"], False)
            if k <= 0:
                continue
            성공, r = tools.매도실행(o["user_id"], o["symbol"], k)
        if not 성공:
            continue                    # VI 가 막 걸렸거나 장이 닫혔다. 다음 틱에 다시
        금액 = r["cost"] if o["kind"] == "buy" else r["income"]
        채움 = o["filled"] + k
        if o["kind"] == "buy":
            끝남 = 채움 >= o["qty"]
        elif o["kind"] == "stop":
            끝남 = True
        else:
            끝남 = (o["qty"] == 0 and r["left"] == 0) or (o["qty"] and 채움 >= o["qty"])
        cursor.execute("UPDATE stock_orders SET filled = ?, amount = amount + ?, status = ?, done_at = ? WHERE id = ?",
                       (채움, 금액, "done" if 끝남 else "open",
                        datetime.now().isoformat(timespec="seconds") if 끝남 else None, o["id"]))
        conn.commit()
        o["filled"] = 채움
        if o["kind"] == "buy":
            글 = (f"✅ 주문 {이름} 체결 — {r['name']} {k:,}주를 평균 {r['fill']:,}원에 샀어요 (총 {r['cost']:,}원)")
        else:
            글 = (f"{'🛑' if o['kind'] == 'stop' else '✅'} 주문 {이름} 체결 — {r['name']} {k:,}주를 평균 {r['fill']:,}원에 팔았어요 "
                 f"(받은 돈 {r['income']:,}원, 손익 {r['profit']:+,}원)")
        if not 끝남:
            남 = (o["qty"] - 채움) if o["qty"] else r.get("left", 0)
            글 += f" · 부분 체결, 남은 {남:,}주는 계속 기다려요"
        알려(o, 글)
    return 알림
