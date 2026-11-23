"""
우리봇v2 AI 함수 호출(Function Calling) 툴 모음

사용법:
    import tools
    tools.init(aapi_객체)          # 봇 시작할 때 한 번 호출 (픽시브용)

    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=tools.모든_선언)],
        ...
    )
"""

import io
import os
import time
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import yt_dlp
from google.genai import types

try:
    from mctools import PINGClient
except ImportError:
    PINGClient = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# run_in_executor 로 다른 스레드에서 실행되므로 check_same_thread=False 필요
conn = sqlite3.connect(os.path.join(BASE_DIR, "bot.db"), check_same_thread=False, timeout=15)
conn.execute("PRAGMA journal_mode=WAL")
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        content TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")
try:
    cursor.execute("ALTER TABLE ai_memory ADD COLUMN label TEXT")      # 라벨: 매 메시지에 라벨 목록만 보내고, 내용은 recall 로 연다
except sqlite3.OperationalError:
    pass
conn.commit()

# 픽시브 객체는 봇에서 넘겨받음
aapi = None

# 노래 다운로드 제한
최대_길이_초 = 600          # 10분
최대_파일_바이트 = 9 * 1024 * 1024   # 9MB (디스코드 10MB 한도 여유분)

# 마인크래프트 서버
MC_주소 = "example.com"     # .env 의 MC_HOST 가 있으면 그걸 쓴다
MC_포트 = 25565

# AI 장기 기억
최대_기억_개수 = 30
최대_기억_길이 = 200
최대_라벨_길이 = 20

# 대출 (봇 파일의 설정과 반드시 일치시킬 것)
최대_대출금 = 100_000_000
대출_이자율 = 0.02
대출_기한_일 = 10

# 채굴 (봇 파일의 설정과 반드시 일치시킬 것)
최대_채굴기 = 10
채굴기_환급률 = 0.70
전기요금_kwh = 500
청구_주기_시간 = 24


def init(pixiv_api):
    """봇 시작 시 호출해서 픽시브 객체를 넘겨준다."""
    global aapi
    aapi = pixiv_api


# ============================================================
# 공통 유틸
# ============================================================

def _get_money(user_id):
    cursor.execute("SELECT money FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return int(float(row[0]))


def 돈변경(user_id, 차액, 커밋=True):
    """
    현금을 SQL 안에서 바꾼다. 봇과 웹이 동시에 바꿔도 값이 사라지지 않는다.
    차액이 음수면 잔고가 모자랄 때 아무것도 안 바꾸고 False 를 돌려준다 (롤백까지 한다).
    """
    차액 = int(차액)
    if 차액 >= 0:
        cursor.execute(
            "UPDATE users SET money = CAST(CAST(money AS INTEGER) + ? AS TEXT) WHERE user_id = ?",
            (차액, user_id),
        )
    else:
        cursor.execute(
            "UPDATE users SET money = CAST(CAST(money AS INTEGER) - ? AS TEXT) "
            "WHERE user_id = ? AND CAST(money AS INTEGER) >= ?",
            (-차액, user_id, -차액),
        )
    if cursor.rowcount == 0:
        if 차액 < 0:
            conn.rollback()        # 안 바뀐 UPDATE 도 쓰기 잠금을 쥐고 있다. 그래서 차감은 항상 맨 먼저 한다
        return False
    if 커밋:
        conn.commit()
    return True


def _find_user_by_name(이름):
    """닉네임으로 user_id 찾기. 정확히 일치 -> 부분 일치 순으로 시도."""
    cursor.execute("SELECT user_id, name FROM users WHERE name = ?", (이름,))
    row = cursor.fetchone()
    if row:
        return row[0], row[1]

    cursor.execute("SELECT user_id, name FROM users WHERE name LIKE ?", (f"%{이름}%",))
    rows = cursor.fetchall()
    if len(rows) == 1:
        return rows[0][0], rows[0][1]
    return None, None


# ============================================================
# 1. 내 잔액 조회
# ============================================================

내_잔액조회_선언 = types.FunctionDeclaration(
    name="get_my_balance",
    description=(
        "현재 대화 중인 유저 본인의 게임 내 보유 금액을 조회한다. "
        "'내 돈 얼마야', '잔액 알려줘', '나 돈 있어?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 내_잔액조회(user_id):
    돈 = _get_money(user_id)
    if 돈 is None:
        return "아직 등록되지 않은 유저입니다. !출석 을 먼저 사용해주세요."
    return f"{돈:,}원"


# ============================================================
# 2. 내 경고 조회
# ============================================================

내_경고조회_선언 = types.FunctionDeclaration(
    name="get_my_warnings",
    description=(
        "현재 대화 중인 유저 본인의 누적 경고 횟수를 조회한다. "
        "'나 경고 몇 번이야', '경고 확인해줘' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 내_경고조회(user_id):
    cursor.execute("SELECT warn_count FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        return "아직 등록되지 않은 유저입니다."
    return f"누적 경고 {row[0]}회"


# ============================================================
# 3. 주식 시장 핵심 로직
#    봇의 !매수 / !매도 와 AI 툴이 모두 이 함수들을 쓴다.
# ============================================================

import math

import 주식              # 주가 엔진·ETF·레버리지·배당/예약 주문 (주식.py)
import json

# ---------- 설정 (봇 파일과 공유) ----------
유통주식_기본 = {0: 2500, 1: 2000}    # 코스닥 / 코스피 종목당 유통 주식 수 (코인은 무제한)
지분_한도 = 0.25                      # 한 사람이 한 종목의 유통량 중 가질 수 있는 최대 비율 (코인 제외)
보유충격_계수 = 0.6                   # 유통량 전부가 사들여지면 +60%
거래_수수료 = 0.001                   # 0.1%
# 세금 (코스피·코스닥 주식만. 코인·레버리지는 안 매긴다)
증권거래세 = 0.002                    # 팔 때 매도 금액의 0.2%
양도세율 = 0.22                       # 1년 동안 판 주식의 순이익(이익 - 손실)에서 공제를 뺀 만큼의 22%. 팔 때마다 정산한다
양도세_공제 = 2_500_000               # 1년에 이만큼의 순이익까지는 양도세가 없다
배당세율 = 0.154                      # 배당소득세 15.4% (배당 때 떼고 준다)

# 코인: 가격은 업비트 실시간 시세를 그대로 쓰고, 거래가 가격을 움직이지 않는다.
#       살 때는 사토시(1 sat = 1원)로 내고, 팔 때는 현금(원)으로 받는다.
코인_유동성배율 = 50                  # 예전 DB 호환용 (쓰지 않음)
코인_시세_유효초 = 300               # 시세가 이보다 오래되면 코인 거래를 막는다

시장_하한 = {0: 500, 1: 1500, 2: 100_000}
시장_이름 = {0: "코스닥", 1: "코스피", 2: "코인"}

_전부 = {"전부", "전체", "다", "모두", "max", "all", "MAX", "ALL"}

# ---------- 가격 기록 (웹 차트용) ----------
cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_history (
        symbol TEXT,
        price TEXT,
        ts TEXT
    )
""")
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_history_symbol_ts ON stock_history (symbol, ts)"
)
conn.commit()

가격기록_보관일 = 7

# 세금 기록: 해마다 사람별로 실현 손익·낸 양도세·거래세·배당
cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_tax (
        user_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        realized INTEGER DEFAULT 0,
        cgt_paid INTEGER DEFAULT 0,
        trade_tax INTEGER DEFAULT 0,
        dividend INTEGER DEFAULT 0,
        dividend_tax INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, year)
    )
""")
conn.commit()


def 가격기록(심볼, 가격, 커밋=True, 거래량=0):
    """차트용으로 주식의 현재가를 캔들(stock_candles)에 남긴다(거래량 더함). 코인은 stock_history 를 쓴다."""
    주식.캔들기록(cursor, 심볼, 가격, 거래량=거래량)
    if 커밋:
        conn.commit()


def 가격기록_정리():
    """보관 기간이 지난 기록을 지운다. 봇 루프에서 가끔 부른다."""
    기준 = (datetime.now() - timedelta(days=가격기록_보관일)).isoformat(timespec="seconds")
    cursor.execute("DELETE FROM stock_history WHERE ts < ?", (기준,))
    주식.캔들정리(cursor)
    conn.commit()


def 시장정보(코드):
    """
    반환: dict 또는 None
      name, price, market, base, supply, held, remain, liquidity, netflow, sector
    supply / remain / sector 는 코인이면 None
    """
    cursor.execute(
        "SELECT s.symbol, s.name, s.price, s.is_premium, s.base_price, s.supply, s.liquidity, s.net_flow, "
        "s.price_updated, s.sector, e.lev, e.fee, e.descr, e.rule FROM stocks s "
        "LEFT JOIN stock_etfs e ON e.symbol = s.symbol AND e.removed = 0 WHERE s.symbol = ? OR s.name = ?",
        (코드.upper().strip(), 코드.strip()),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    심볼, 이름, 가격, 시장, 기준, 유통, 유동, 흐름, 갱신, 업종, 배율, 보수, 설명, 규칙 = row
    현재가 = int(float(가격))
    기준가 = int(float(기준 or 0)) or 현재가

    if 배율 is not None:
        # ETF: LP 가 있다고 보고 NAV(기준가) 그대로 사고판다. 유통량·지분 한도·업종 없음 (주식.py ETF 칸)
        try:
            규칙 = json.loads(규칙 or "{}")
        except ValueError:
            규칙 = {}
        return {
            "symbol": 심볼, "name": 이름, "price": 현재가, "market": 시장,
            "base": max(1, round(float(기준 or 0))) or 현재가, "supply": None, "held": _보유합(심볼),
            "remain": None, "liquidity": 0, "netflow": 0, "updated": 갱신, "sector": None,
            "etf": {"lev": 배율, "fee": 보수 or 0.0, "desc": 설명 or "", "rule": 규칙},
        }

    cursor.execute(
        "SELECT COALESCE(SUM(CAST(quantity AS INTEGER)), 0) FROM portfolio WHERE symbol = ?",
        (심볼,),
    )
    보유합 = cursor.fetchone()[0] or 0

    if 시장 in (0, 1):
        유통량 = int(float(유통 or 0)) or 유통주식_기본.get(시장, 500)
        잔여 = max(0, 유통량 - 보유합)
        if not int(float(기준 or 0)):
            # 기준가가 비어 있는 종목(예전 !주식추가 로 상장)은 지금 가격에서 보유량 영향을 빼서
            # 기준가를 한 번 정하고 저장한다. 비워두면 매번 올라간 현재가를 기준가로 착각해
            # 사고팔 때마다 주가가 1.3배씩 불어나는 돈 복사가 된다.
            배율 = 1 + 보유충격_계수 * math.sqrt(max(0.0, min(1.0, 보유합 / 유통량)))
            기준가 = max(시장_하한.get(시장, 500), int(현재가 / 배율))
            cursor.execute("UPDATE stocks SET base_price = ? WHERE symbol = ?", (str(기준가), 심볼))
            conn.commit()
        if not int(float(유통 or 0)):
            cursor.execute("UPDATE stocks SET supply = ? WHERE symbol = ?", (str(유통량), 심볼))
            conn.commit()
    else:
        유통량 = None
        잔여 = None

    return {
        "etf": None,
        "symbol": 심볼,
        "name": 이름,
        "price": 현재가,
        "market": 시장,
        "base": 기준가,
        "supply": 유통량,
        "held": 보유합,
        "remain": 잔여,
        "liquidity": int(float(유동 or 0)) or 현재가 * 코인_유동성배율,
        "netflow": int(float(흐름 or 0)),
        "updated": 갱신,
        "sector": 주식.종목업종(업종, 이름) if 시장 in (0, 1) else None,
    }


def _보유합(심볼):
    cursor.execute("SELECT COALESCE(SUM(CAST(quantity AS INTEGER)), 0) FROM portfolio WHERE symbol = ?", (심볼,))
    return cursor.fetchone()[0] or 0


def 거래세율(정보):
    """팔 때 떼는 증권거래세율. 코인·ETF 는 0"""
    return 0 if 정보["market"] == 2 or 정보.get("etf") else 증권거래세


def 코인시세_확인(정보):
    """코인 시세가 최신이면 None, 오래됐으면 오류 문구를 돌려준다."""
    if 정보["market"] != 2:
        return None
    if not 정보.get("updated"):
        return "코인 시세를 아직 받아오지 못했습니다. 잠시 후 다시 시도해주세요."
    try:
        경과 = (datetime.now() - datetime.fromisoformat(정보["updated"])).total_seconds()
    except ValueError:
        return "코인 시세 정보가 올바르지 않습니다."
    if 경과 > 코인_시세_유효초:
        return f"코인 시세가 {int(경과 // 60)}분째 갱신되지 않아 거래를 막았습니다."
    return None


def 가격계산(기준가, 시장, 보유합=0, 유통량=None, 순매수=0, 유동성=0):
    """기준가 위에 수급 충격을 얹은 현재가. 코인은 실시간 시세 그대로."""
    하한 = 시장_하한.get(시장, 500)
    if 시장 in (0, 1) and 유통량:
        비율 = max(0.0, min(1.0, 보유합 / 유통량))
        return max(하한, int(기준가 * (1 + 보유충격_계수 * math.sqrt(비율))))
    return max(1, 기준가)


def _누적(기준가, 보유, 유통량):
    """보유량 0 → 보유 까지 가격 곡선 아래 넓이 (적분)"""
    return 기준가 * (보유 + 보유충격_계수 * (2 / 3) * 보유 ** 1.5 / math.sqrt(유통량))


def _예상체결(정보, 수량, 매수):
    """
    DB를 바꾸지 않고 (체결 평균가, 거래 후 가격, 변경내용) 을 계산한다.

    주식: 체결가 = 거래 구간의 가격 곡선 평균(적분).
          나눠서 사든 한 번에 사든 총비용이 같아서 쪼개기 꼼수가 통하지 않는다.
    코인: 실시간 시세 그대로 체결되고 가격은 변하지 않는다.
    """
    if 정보["market"] == 2:
        return 정보["price"], 정보["price"], {}
    if 정보.get("etf"):                      # ETF: NAV 그대로, 가격이 안 움직인다
        return 정보["base"], 정보["base"], {"held": 정보["held"] + (수량 if 매수 else -수량)}

    기준, 유통 = 정보["base"], 정보["supply"]
    h0 = 정보["held"]
    h1 = h0 + 수량 if 매수 else h0 - 수량
    lo, hi = min(h0, h1), max(h0, h1)
    if hi == lo:
        return 정보["price"], 정보["price"], {"held": h1}

    평균 = (_누적(기준, hi, 유통) - _누적(기준, lo, 유통)) / (hi - lo)
    후 = 가격계산(기준, 정보["market"], h1, 유통)
    return max(1, int(round(평균))), 후, {"held": h1}


def _사토시잔액(user_id):
    cursor.execute("SELECT satoshi FROM mining_balance WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return int(float(row[0] or 0)) if row else 0


def _사토시설정(user_id, 값):
    cursor.execute("INSERT OR IGNORE INTO mining_balance (user_id) VALUES (?)", (user_id,))
    cursor.execute(
        "UPDATE mining_balance SET satoshi = ? WHERE user_id = ?", (str(값), user_id)
    )


def 사토시변경(user_id, 차액, 커밋=True):
    """사토시를 SQL 안에서 바꾼다. 차감할 때 모자라면 False (롤백)"""
    차액 = int(차액)
    cursor.execute("INSERT OR IGNORE INTO mining_balance (user_id) VALUES (?)", (user_id,))
    if 차액 >= 0:
        cursor.execute(
            "UPDATE mining_balance SET satoshi = CAST(CAST(COALESCE(satoshi, '0') AS INTEGER) + ? AS TEXT) WHERE user_id = ?",
            (차액, user_id),
        )
    else:
        cursor.execute(
            "UPDATE mining_balance SET satoshi = CAST(CAST(satoshi AS INTEGER) - ? AS TEXT) "
            "WHERE user_id = ? AND CAST(COALESCE(satoshi, '0') AS INTEGER) >= ?",
            (-차액, user_id, -차액),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
    if 커밋:
        conn.commit()
    return True


def _보유차감(user_id, 심볼, 수량):
    """보유 수량을 SQL 안에서 뺀다. 모자라면 False (롤백). 0 이 되면 줄을 지운다"""
    cursor.execute(
        "UPDATE portfolio SET quantity = CAST(CAST(quantity AS INTEGER) - ? AS TEXT) "
        "WHERE user_id = ? AND symbol = ? AND CAST(quantity AS INTEGER) >= ?",
        (수량, user_id, 심볼, 수량),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        return False
    cursor.execute(
        "DELETE FROM portfolio WHERE user_id = ? AND symbol = ? AND CAST(quantity AS INTEGER) <= 0",
        (user_id, 심볼),
    )
    return True


def _보유추가(user_id, 심볼, 수량, 매입액):
    cursor.execute(
        "INSERT INTO portfolio (user_id, symbol, quantity, buyed_val) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, symbol) DO UPDATE SET "
        "quantity = CAST(CAST(quantity AS INTEGER) + ? AS TEXT), "
        "buyed_val = CAST(CAST(COALESCE(buyed_val, '0') AS INTEGER) + ? AS TEXT)",
        (user_id, 심볼, str(수량), str(매입액), 수량, 매입액),
    )


def _내보유(user_id, 심볼):
    cursor.execute(
        "SELECT quantity FROM portfolio WHERE user_id = ? AND symbol = ?", (user_id, 심볼)
    )
    row = cursor.fetchone()
    return int(float(row[0])) if row else 0


def 지분한도(정보):
    """한 사람이 가질 수 있는 최대 주식 수. 코인은 None (제한 없음)"""
    if 정보["market"] == 2 or not 정보["supply"]:
        return None
    return int(정보["supply"] * 지분_한도)


def 추가매수가능(정보, user_id):
    """
    이 사람이 이 종목을 지금 더 살 수 있는 최대 수량.
    남은 유통량과 지분 한도 중 작은 쪽. 코인은 None (제한 없음)
    """
    한도 = 지분한도(정보)
    if 한도 is None:
        return None
    return max(0, min(정보["remain"], 한도 - _내보유(user_id, 정보["symbol"])))


def _최대매수수량(정보, 예산, 수량상한=None):
    """예산(주식은 원, 코인은 사토시) 안에서 살 수 있는 최대 수량"""
    if 정보["market"] == 2:
        return 예산 // max(1, 정보["price"])

    상한 = 예산 // max(1, 정보["price"])
    if 정보["remain"] is not None:
        상한 = min(상한, 정보["remain"])
    if 수량상한 is not None:
        상한 = min(상한, 수량상한)
    lo, hi = 0, max(0, 상한)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        체결가, _, _ = _예상체결(정보, mid, True)
        if int(체결가 * mid * (1 + 거래_수수료)) <= 예산:
            lo = mid
        else:
            hi = mid - 1
    return lo


def 매수실행(user_id, 종목, 수량):
    """
    반환: (성공여부, 결과 dict 또는 오류 문자열)
    결과 dict: name, symbol, qty, fill, cost, fee, before, after, cash, remain, unit
      unit 이 "sat" 이면 코인 (사토시로 결제), "원" 이면 주식
    """
    정보 = 시장정보(종목 or "")
    if 정보 is None:
        return False, f"'{종목}'은(는) 존재하지 않는 종목입니다."

    코인 = 정보["market"] == 2
    if 코인:
        오류 = 코인시세_확인(정보)
        if 오류:
            return False, 오류
        예산 = _사토시잔액(user_id)
        단위 = "sat"
    else:
        막힘 = 주식.거래불가() or 주식.거래정지(cursor, 정보["symbol"])
        if 막힘:
            return False, 막힘
        예산 = _get_money(user_id)
        if 예산 is None:
            return False, "아직 등록되지 않은 유저입니다. !출석 을 먼저 사용해주세요."
        단위 = "원"
        if 정보["remain"] is not None and 정보["remain"] <= 0:
            return False, f"{정보['name']}({정보['symbol']}) 은(는) 유통 물량이 모두 팔려 살 수 없습니다."

    가능 = None if 코인 else 추가매수가능(정보, user_id)
    if 가능 == 0 and 정보["remain"]:
        한도 = 지분한도(정보)
        return False, (
            f"{정보['name']}은(는) 한 사람이 최대 {한도:,}주(유통량의 {int(지분_한도 * 100)}%)까지만 "
            f"가질 수 있습니다. 이미 {_내보유(user_id, 정보['symbol']):,}주를 보유 중입니다."
        )

    if str(수량).strip() in _전부:
        개수 = _최대매수수량(정보, 예산, 가능)
        if 개수 <= 0:
            return False, (
                f"{정보['name']} 1개에 {정보['price']:,}{단위}인데 "
                f"보유 {예산:,}{단위}로는 살 수 없습니다."
            )
    else:
        try:
            개수 = int(str(수량).replace(",", ""))
        except (ValueError, TypeError):
            return False, "수량은 숫자이거나 '전부' 여야 합니다."
        if 개수 <= 0:
            return False, "1개 이상만 매수할 수 있습니다."
        if not 코인 and 정보["remain"] is not None and 개수 > 정보["remain"]:
            return False, (
                f"{정보['name']}은(는) {정보['remain']:,}주만 남아 있습니다. "
                f"(유통량 {정보['supply']:,}주)"
            )
        if 가능 is not None and 개수 > 가능:
            return False, (
                f"{정보['name']}은(는) 한 사람이 최대 {지분한도(정보):,}주"
                f"(유통량의 {int(지분_한도 * 100)}%)까지만 가질 수 있습니다. "
                f"지금 더 살 수 있는 수량은 {가능:,}주입니다."
            )

    체결가, 후, 변경 = _예상체결(정보, 개수, True)
    원금 = 체결가 * 개수
    수수료 = 0 if 코인 else int(원금 * 거래_수수료)
    총액 = 원금 + 수수료

    if 예산 < 총액:
        return False, (
            f"{'사토시' if 코인 else '잔액'}가 부족합니다. "
            f"필요 {총액:,}{단위} / 보유 {예산:,}{단위}"
        )

    # --- 결제 (잔고 확인과 차감을 SQL 하나로) ---
    결제됨 = 사토시변경(user_id, -총액, 커밋=False) if 코인 else 돈변경(user_id, -총액, 커밋=False)
    if not 결제됨:
        return False, f"{'사토시' if 코인 else '잔액'}가 부족합니다. (필요 {총액:,}{단위})"

    _보유추가(user_id, 정보["symbol"], 개수, 총액)

    if not 코인:
        cursor.execute("UPDATE stocks SET price = ? WHERE symbol = ?", (str(후), 정보["symbol"]))
        가격기록(정보["symbol"], 후, 커밋=False, 거래량=개수)
    conn.commit()

    return True, {
        "name": 정보["name"], "symbol": 정보["symbol"], "qty": 개수,
        "fill": 체결가, "cost": 총액, "fee": 수수료,
        "before": 정보["price"], "after": 후, "cash": 예산 - 총액,
        "remain": None if 정보["remain"] is None else 정보["remain"] - 개수,
        "unit": 단위,
    }


def 청산가치(정보, 수량):
    """
    지금 수량만큼 전부 판다면 실제로 받는 돈.
    내가 팔면서 가격을 끌어내리는 것과 수수료까지 반영한다.
    반환: (평균 매도가, 수령액)
    """
    if 수량 <= 0:
        return 0, 0
    if 정보["market"] != 2:
        수량 = min(수량, 정보["held"])        # 내 몫이 전체 보유량을 넘을 수는 없다
        if 수량 <= 0:
            return 정보["price"], 0
    평균가, _, _ = _예상체결(정보, 수량, False)
    세 = 거래세율(정보)
    return 평균가, int(평균가 * 수량 * (1 - 거래_수수료 - 세))


def 매도실행(user_id, 종목, 수량=None):
    """
    반환: (성공여부, 결과 dict 또는 오류 문자열)
    코인도 팔면 현금(원)으로 받는다.
    """
    정보 = 시장정보(종목 or "")
    if 정보 is None:
        return False, f"'{종목}'은(는) 존재하지 않는 종목입니다."

    코인 = 정보["market"] == 2
    if 코인:
        오류 = 코인시세_확인(정보)
        if 오류:
            return False, 오류
    else:
        막힘 = 주식.거래불가() or 주식.거래정지(cursor, 정보["symbol"])
        if 막힘:
            return False, 막힘

    cursor.execute(
        "SELECT quantity, buyed_val FROM portfolio WHERE user_id = ? AND symbol = ?",
        (user_id, 정보["symbol"]),
    )
    보유 = cursor.fetchone()
    if 보유 is None or int(float(보유[0])) <= 0:
        return False, f"{정보['name']}({정보['symbol']}) 을(를) 보유하고 있지 않습니다."

    보유수량 = int(float(보유[0]))
    매입액 = int(float(보유[1] or 0))

    if 수량 is None or str(수량).strip() in _전부:
        개수 = 보유수량
    else:
        try:
            개수 = int(str(수량).replace(",", ""))
        except (ValueError, TypeError):
            return False, "수량은 숫자이거나 '전부' 여야 합니다."
        if 개수 <= 0:
            return False, "1개 이상만 매도할 수 있습니다."
        if 개수 > 보유수량:
            return False, f"보유 수량보다 많이 팔 수 없습니다. (보유 {보유수량:,}개)"

    체결가, 후, 변경 = _예상체결(정보, 개수, False)
    원금 = 체결가 * 개수
    수수료 = int(원금 * 거래_수수료)
    거래세 = int(원금 * 거래세율(정보))
    수령 = 원금 - 수수료 - 거래세

    남은 = 보유수량 - 개수
    매입분 = 매입액 if 남은 == 0 else 매입액 * 개수 // 보유수량
    if not _보유차감(user_id, 정보["symbol"], 개수):
        return False, "그 사이 보유 수량이 바뀌었습니다. 다시 시도해주세요."
    # 양도세: 올해 순이익이 공제를 넘은 만큼의 22%. 손해를 보고 팔면 올해 낸 양도세에서 그만큼 돌려준다
    양도세 = 0 if (코인 or 정보.get("etf")) else _양도세정산(user_id, 수령 - 매입분, 거래세)      # ETF 는 국내 주식형처럼 비과세
    수령 -= 양도세
    if 남은 > 0:
        cursor.execute(
            "UPDATE portfolio SET buyed_val = CAST(MAX(0, CAST(buyed_val AS INTEGER) - ?) AS TEXT) "
            "WHERE user_id = ? AND symbol = ?",
            (매입분, user_id, 정보["symbol"]),
        )

    돈변경(user_id, 수령, 커밋=False)
    현금 = _get_money(user_id) or 0

    if not 코인:
        cursor.execute("UPDATE stocks SET price = ? WHERE symbol = ?", (str(후), 정보["symbol"]))
        가격기록(정보["symbol"], 후, 커밋=False, 거래량=개수)
    conn.commit()

    return True, {
        "name": 정보["name"], "symbol": 정보["symbol"], "qty": 개수,
        "fill": 체결가, "income": 수령, "fee": 수수료, "profit": 수령 - 매입분,
        "tax": 거래세, "cgt": 양도세, "etf": bool(정보.get("etf")),
        "before": 정보["price"], "after": 후, "cash": 현금, "left": 남은,
    }


def _세금줄(user_id, 연도):
    cursor.execute("INSERT OR IGNORE INTO stock_tax (user_id, year) VALUES (?, ?)", (user_id, 연도))


def _양도세정산(user_id, 손익, 거래세=0):
    """
    이번 매도의 손익(수수료·거래세 뺀 뒤)을 올해 실현 손익에 더하고, 지금까지 내야 할 양도세와 낸 양도세의 차이를 돌려준다.
    양수면 이번에 더 떼고, 음수면 돌려준다 (손해를 보고 팔면 올해 이미 낸 세금에서 환급). 커밋은 부른 쪽에서.
    """
    연도 = datetime.now().year
    _세금줄(user_id, 연도)
    cursor.execute("SELECT realized, cgt_paid FROM stock_tax WHERE user_id = ? AND year = ?", (user_id, 연도))
    실현, 낸 = cursor.fetchone()
    실현 = int(실현 or 0) + int(손익)
    내야 = int(max(0, 실현 - 양도세_공제) * 양도세율)
    차이 = 내야 - int(낸 or 0)
    cursor.execute(
        "UPDATE stock_tax SET realized = realized + ?, cgt_paid = cgt_paid + ?, trade_tax = trade_tax + ? "
        "WHERE user_id = ? AND year = ?", (int(손익), 차이, int(거래세), user_id, 연도))
    return 차이


def 배당기록(user_id, 받은돈, 뗀세금, 커밋=False):
    """배당 받은 돈을 올해 세금 기록에 더한다"""
    연도 = datetime.now().year
    _세금줄(user_id, 연도)
    cursor.execute("UPDATE stock_tax SET dividend = dividend + ?, dividend_tax = dividend_tax + ? "
                   "WHERE user_id = ? AND year = ?", (int(받은돈), int(뗀세금), user_id, 연도))
    if 커밋:
        conn.commit()


def 세금조회(user_id, 연도=None):
    """{"year", "realized", "cgt", "trade_tax", "dividend", "dividend_tax", "free_left", "rate", "deduction"}"""
    연도 = 연도 or datetime.now().year
    cursor.execute("SELECT realized, cgt_paid, trade_tax, dividend, dividend_tax FROM stock_tax "
                   "WHERE user_id = ? AND year = ?", (user_id, 연도))
    행 = cursor.fetchone() or (0, 0, 0, 0, 0)
    실현, 양도, 거래, 배당, 배당세 = (int(x or 0) for x in 행)
    return {"year": 연도, "realized": 실현, "cgt": 양도, "trade_tax": 거래, "dividend": 배당, "dividend_tax": 배당세,
            "free_left": max(0, 양도세_공제 - 실현), "rate": 양도세율, "deduction": 양도세_공제,
            "trade_rate": 증권거래세, "dividend_rate": 배당세율}


# ============================================================
# 3-1. 주가 조회
# ============================================================

주가조회_선언 = types.FunctionDeclaration(
    name="get_stock_price",
    description=(
        "주식 종목의 현재 가격, 전일 대비 등락률, 업종, 남은 유통 수량, 장이 열려 있는지를 조회한다. "
        "같은 업종 종목은 실제 한국 주식의 그 업종처럼 같이 오르내린다. "
        "종목 코드나 이름을 주면 그 종목만, "
        "생략하면 전체 종목 목록을 반환한다. '삼성전자 주가 얼마야', "
        "'주식 목록 보여줘', '몇 주 남았어?' 같은 질문에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "종목 코드 또는 종목명 (예: SAMSUNG, 삼성전자). 생략하면 전체 목록",
            }
        },
    },
)


def _한줄(정보, 전일=None):
    줄 = f"{정보['name']}({정보['symbol']}): {정보['price']:,}원"
    if 정보.get("etf"):
        줄 += f" [ETF {주식.배율이름(정보['etf']['lev'])} · {주식.지수이름(정보['etf']['rule'])}]"
    if 정보.get("sector"):
        줄 += f" [{정보['sector']}]"
    기준 = (전일 or {}).get(정보["symbol"])
    if 기준:
        줄 += f" (전일 대비 {(정보['price'] - 기준) / 기준 * 100:+.2f}%)"
    if 정보["remain"] is not None:
        줄 += f" (잔여 {정보['remain']:,}/{정보['supply']:,}주)"
    return 줄


def 주가조회(symbol=None):
    if symbol and symbol.strip():
        정보 = 시장정보(symbol)
        if 정보 is None:
            return f"'{symbol}'은(는) 존재하지 않는 종목입니다."
        if 정보["market"] == 2:
            return _한줄(정보)
        return _한줄(정보, 주식.전일종가들(cursor)) + f" [{주식.장상태()['text']}]"

    전일 = 주식.전일종가들(cursor)
    cursor.execute("SELECT symbol, is_premium FROM stocks ORDER BY is_premium DESC, symbol")
    묶음 = {}
    for 심볼, 시장 in cursor.fetchall():
        정보 = 시장정보(심볼)
        if 정보:
            묶음.setdefault(시장, []).append(_한줄(정보, 전일))
    상태 = 주식.장상태()
    줄 = [f"주식 시장: {상태['text']} (장 시간 {상태['hours']}, 코인은 24시간)"]
    for 시장 in (1, 0, 2):
        if 묶음.get(시장):
            줄.append(f"[{시장_이름[시장]}]")
            줄 += 묶음[시장]
    return "\n".join(줄) if 줄 else "상장된 종목이 없습니다."


# ============================================================
# 4. 주식 매수
# ============================================================

주식매수_선언 = types.FunctionDeclaration(
    name="buy_stock",
    description=(
        "현재 대화 중인 유저 본인의 돈으로 주식을 매수한다. "
        "'삼성전자 3주 사줘', '애플 매수해줘' 같은 요청에 사용한다. "
        "코스피·코스닥 종목은 유통 수량이 정해져 있어 남은 만큼만 살 수 있고, "
        "많이 살수록 가격이 올라 체결가가 비싸진다. "
        f"주식은 장이 열려 있을 때({주식.시간표()})만 사고팔 수 있다. "
        "코인은 실제 거래소(업비트) 실시간 시세로 24시간 거래되며 가격 충격이 없다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드 또는 종목명"},
            "amount": {"type": "integer", "description": "매수할 주식 수량 (1 이상)"},
        },
        "required": ["symbol", "amount"],
    },
)


def 주식매수(user_id, symbol, amount):
    성공, 결과 = 매수실행(user_id, symbol, amount)
    if not 성공:
        return 결과
    r = 결과
    u = r["unit"]
    답 = (
        f"{r['name']}({r['symbol']}) {r['qty']:,}개를 개당 {r['fill']:,}{u}에 매수했습니다. "
        f"총 {r['cost']:,}{u}"
        + (f" (수수료 {r['fee']:,}{u})" if r["fee"] else "")
        + f", 남은 {'사토시' if u == 'sat' else '잔액'} {r['cash']:,}{u}."
    )
    if r["before"] != r["after"]:
        답 += f" 주가 {r['before']:,} → {r['after']:,}원."
    if r["remain"] is not None:
        답 += f" 남은 유통량 {r['remain']:,}주."
    return 답


# ============================================================
# 5. 주식 매도
# ============================================================

주식매도_선언 = types.FunctionDeclaration(
    name="sell_stock",
    description=(
        "현재 대화 중인 유저 본인이 보유한 주식을 매도한다. "
        "'삼성전자 2주 팔아줘', '애플 전부 매도' 같은 요청에 사용한다. "
        "수량을 생략하면 보유한 전량을 매도한다. 많이 팔수록 가격이 내려간다. "
        f"주식은 팔 때 거래세 {증권거래세 * 100:g}%와 양도세(1년 순이익 {양도세_공제:,}원 넘는 만큼의 {양도세율 * 100:g}%)를 뗀다. "
        "VI·서킷브레이커로 거래가 멈춰 있으면 못 판다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드 또는 종목명"},
            "amount": {"type": "integer", "description": "매도할 수량. 생략하면 전량"},
        },
        "required": ["symbol"],
    },
)


def 주식매도(user_id, symbol, amount=None):
    성공, 결과 = 매도실행(user_id, symbol, amount)
    if not 성공:
        return 결과
    r = 결과
    부호 = "+" if r["profit"] >= 0 else ""
    답 = (
        f"{r['name']}({r['symbol']}) {r['qty']:,}개를 개당 {r['fill']:,}원에 매도했습니다. "
        f"{r['income']:,}원 획득 (수수료 {r['fee']:,}원"
        + (f", 거래세 {r['tax']:,}원" if r.get("tax") else "")
        + (f", 양도세 {r['cgt']:,}원" if r.get("cgt", 0) > 0 else f", 양도세 환급 {-r['cgt']:,}원" if r.get("cgt", 0) < 0 else "")
        + f", 손익 {부호}{r['profit']:,}원), "
        f"현재 잔액 {r['cash']:,}원."
    )
    if r["before"] != r["after"]:
        답 += f" 주가 {r['before']:,} → {r['after']:,}원."
    return 답


# ============================================================
# 6. 다른 유저 AI 기억 조회 (공개 허용한 유저만)
# ============================================================

기억조회_선언 = types.FunctionDeclaration(
    name="get_user_memory",
    description=(
        "다른 유저가 AI와 나눈 최근 대화 기록을 조회한다. "
        "해당 유저가 !기억공개 허용 을 한 경우에만 조회할 수 있다. "
        "'철수가 너랑 무슨 얘기 했어?' 같은 질문에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {
                "type": "string",
                "description": "조회할 유저의 디스코드 닉네임",
            }
        },
        "required": ["nickname"],
    },
)


def 기억조회(nickname):
    대상_id, 대상_이름 = _find_user_by_name(nickname)
    if 대상_id is None:
        return f"'{nickname}'(이)라는 유저를 찾지 못했습니다."

    cursor.execute("SELECT user_id FROM memory_public WHERE user_id = ?", (대상_id,))
    if cursor.fetchone() is None:
        return f"{대상_이름}님은 기억 공개를 허용하지 않았습니다."

    cursor.execute(
        "SELECT role, content FROM ai_history WHERE user_id = ? "
        "ORDER BY rowid DESC LIMIT 6",
        (대상_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return f"{대상_이름}님의 대화 기록이 없습니다."

    rows.reverse()
    줄 = []
    for role, content in rows:
        말한이 = 대상_이름 if role == "user" else "나"
        내용 = content[:200]
        줄.append(f"{말한이}: {내용}")
    return f"{대상_이름}님과의 최근 대화:\n" + "\n".join(줄)

# 픽시브 기능은 이 저장소에 없다 (빈 껍데기만)
픽시브검색_선언 = types.FunctionDeclaration(
    name="search_pixiv",
    description="픽시브 검색. 이 버전에는 없는 기능이라 늘 '없음'을 돌려준다.",
    parameters={"type": "object", "properties": {"query": {"type": "string", "description": "검색어"}},
                "required": ["query"]},
)


def 픽시브검색(query=None, latest=False, kind="illust"):
    return {"text": "이 버전의 봇에는 픽시브 기능이 없어요."}




# ============================================================
# 8. 내 포트폴리오 조회
# ============================================================

내_포트폴리오_선언 = types.FunctionDeclaration(
    name="get_my_portfolio",
    description=(
        "현재 대화 중인 유저 본인이 보유한 주식 목록을 조회한다. "
        "종목별 보유 수량, 매입 금액, 현재 평가 금액, 손익을 알려준다. "
        "'내 주식 보여줘', '내 포트폴리오', '나 뭐 들고 있어?', "
        "'수익 났어?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 내_포트폴리오(user_id):
    cursor.execute(
        "SELECT symbol, quantity, buyed_val FROM portfolio WHERE user_id = ?",
        (user_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return "보유 중인 주식이 없습니다."

    줄 = []
    총매입 = 0
    총평가 = 0

    for symbol, quantity, buyed_val in rows:
        수량 = int(float(quantity))
        매입액 = int(float(buyed_val))
        정보 = 시장정보(symbol)
        if 정보 is None or 수량 <= 0:
            continue

        매도평균, 평가액 = 청산가치(정보, 수량)
        손익 = 평가액 - 매입액
        수익률 = (손익 / 매입액 * 100) if 매입액 else 0
        평단 = 매입액 // 수량 if 수량 else 0

        부호 = "+" if 손익 >= 0 else ""
        줄.append(
            f"{정보['name']}({symbol}) {수량:,}개 | 평단 {평단:,}원 · 현재가 {정보['price']:,}원 | "
            f"지금 전부 팔면 {평가액:,}원 (평균 {매도평균:,}원) | 손익 {부호}{손익:,}원 ({부호}{수익률:.1f}%)"
        )
        총매입 += 매입액
        총평가 += 평가액

    if not 줄:
        return "보유 중인 주식이 없습니다."

    총손익 = 총평가 - 총매입
    총수익률 = (총손익 / 총매입 * 100) if 총매입 else 0
    부호 = "+" if 총손익 >= 0 else ""

    현금 = _get_money(user_id)
    현금표시 = f"{현금:,}원" if 현금 is not None else "알 수 없음"

    return (
        "\n".join(줄)
        + f"\n---\n평가액은 지금 전부 팔았을 때 실제로 받는 돈 기준 (가격 하락·수수료 반영)"
        + f"\n총 매입 {총매입:,}원 / 총 평가 {총평가:,}원 / "
        f"총 손익 {부호}{총손익:,}원 ({부호}{총수익률:.1f}%)"
        + f"\n보유 현금 {현금표시} / 총 자산 {총평가 + (현금 or 0):,}원"
    )


# ============================================================
# 9. 유튜브 음원 다운로드
# ============================================================

노래다운_선언 = types.FunctionDeclaration(
    name="download_song",
    description=(
        "유튜브에서 노래를 검색해 mp3 파일로 받아 채팅에 올려준다. "
        "'그 노래 파일로 줘', '노래 다운받아줘', 'mp3로 보내줘' 같은 요청에 사용한다. "
        "단순히 음성채널에서 재생해달라는 요청에는 쓰지 않는다. "
        "10분이 넘는 영상이나 파일이 너무 큰 경우는 받을 수 없다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "검색할 노래 제목이나 아티스트. 유튜브 URL도 가능하다.",
            }
        },
        "required": ["query"],
    },
)


def 노래다운(query):
    """
    반환값: dict
        성공 시 {"text": 설명, "image": bytes, "filename": str}
        실패 시 {"text": 사유}
    (봇 쪽 첨부 처리 형식을 그대로 재사용)
    """
    작업폴더 = os.path.join(BASE_DIR, "_dl_tmp")
    os.makedirs(작업폴더, exist_ok=True)

    검색대상 = query.strip()
    if not 검색대상.startswith("http"):
        검색대상 = f"ytsearch:{검색대상}"

    쿠키 = os.path.join(BASE_DIR, "cookies.txt")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(작업폴더, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    deno = os.path.expanduser("~/.deno/bin/deno")
    if os.path.exists(deno):
        opts["js_runtimes"] = {"deno": {"path": deno}}
    if os.path.exists(쿠키):
        opts["cookies"] = 쿠키

    파일경로 = None
    try:
        # 1단계: 정보만 받아서 길이 확인
        with yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(검색대상, download=False)

        if "entries" in info:
            if not info["entries"]:
                return {"text": f"'{query}'에 대한 검색 결과가 없습니다."}
            info = info["entries"][0]

        제목 = info.get("title", "알 수 없음")
        길이 = info.get("duration") or 0

        if 길이 > 최대_길이_초:
            분 = 길이 // 60
            return {"text": f"「{제목}」은(는) {분}분짜리라 너무 깁니다. {최대_길이_초 // 60}분 이하만 받을 수 있습니다."}

        # 2단계: 실제 다운로드
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(info.get("webpage_url") or 검색대상, download=True)
            if "entries" in info:
                info = info["entries"][0]
            파일경로 = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"

        if not os.path.exists(파일경로):
            return {"text": "파일을 받지 못했습니다."}

        크기 = os.path.getsize(파일경로)
        if 크기 > 최대_파일_바이트:
            mb = 크기 / 1024 / 1024
            return {"text": f"「{제목}」 파일이 {mb:.1f}MB로 너무 큽니다. 디스코드에 올릴 수 없습니다."}

        with open(파일경로, "rb") as f:
            데이터 = f.read()

        안전한제목 = "".join(c for c in 제목 if c.isalnum() or c in " -_()[]").strip()[:80]
        if not 안전한제목:
            안전한제목 = "song"

        return {
            "text": f"「{제목}」 파일을 올렸습니다. ({크기 / 1024 / 1024:.1f}MB)",
            "image": 데이터,
            "filename": f"{안전한제목}.mp3",
        }

    except Exception as e:
        return {"text": f"노래를 받는 중 오류가 발생했습니다: {str(e)[:150]}"}

    finally:
        if 파일경로 and os.path.exists(파일경로):
            try:
                os.remove(파일경로)
            except OSError:
                pass


# ============================================================
# 10. 마인크래프트 서버 상태 조회
# ============================================================

마크상태_선언 = types.FunctionDeclaration(
    name="get_mc_server_status",
    description=(
        "마인크래프트 서버의 현재 상태를 조회한다. "
        "서버가 켜져 있는지, 지금 몇 명이 접속해 있는지, 누가 접속해 있는지, "
        "서버 버전이 무엇인지 알려준다. "
        "'마크 서버 켜져 있어?', '지금 누구 접속했어?', '서버에 몇 명 있어?' "
        "같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 마크상태():
    if PINGClient is None:
        return "마인크래프트 조회 기능이 설치되어 있지 않습니다. (pip install mctools)"

    ping = None
    try:
        ping = PINGClient(os.environ.get("MC_HOST", MC_주소), int(os.environ.get("MC_PORT", MC_포트)), timeout=5)
        stats = ping.get_stats()
    except Exception:
        return f"서버({os.environ.get('MC_HOST', MC_주소)})가 꺼져 있거나 응답하지 않습니다."
    finally:
        if ping is not None:
            try:
                ping.stop()
            except Exception:
                pass

    if not isinstance(stats, dict):
        return "서버 응답을 해석하지 못했습니다."

    def _평탄화(값):
        """description 이 문자열 / dict / list 어떤 형태로 와도 문자열로 만든다."""
        if 값 is None:
            return ""
        if isinstance(값, str):
            return 값
        if isinstance(값, dict):
            글 = 값.get("text", "")
            for 조각 in 값.get("extra", []) or []:
                글 += _평탄화(조각)
            return 글
        if isinstance(값, list):
            return "".join(_평탄화(조각) for 조각 in 값)
        return str(값)

    줄 = []

    # --- 버전 ---
    버전정보 = stats.get("version")
    if isinstance(버전정보, dict):
        버전 = 버전정보.get("name", "알 수 없음")
    else:
        버전 = str(버전정보) if 버전정보 else "알 수 없음"

    # --- 인원 ---
    players = stats.get("players")
    현재 = 최대 = None
    샘플 = []

    if isinstance(players, dict):
        현재 = players.get("online")
        최대 = players.get("max")
        raw샘플 = players.get("sample")
        if isinstance(raw샘플, list):
            샘플 = raw샘플
    elif isinstance(players, list):
        샘플 = players
        현재 = len(players)

    줄.append(f"서버 상태: 온라인 ({버전})")
    if 현재 is not None and 최대 is not None:
        줄.append(f"접속 인원: {현재}/{최대}명")
    elif 현재 is not None:
        줄.append(f"접속 인원: {현재}명")

    # --- 접속자 목록 ---
    이름들 = []
    for p in 샘플:
        if isinstance(p, dict):
            이름 = p.get("name")
        else:
            이름 = str(p)
        if 이름:
            이름들.append(이름)

    if 이름들:
        줄.append("접속자: " + ", ".join(이름들[:30]))
    elif 현재:
        줄.append("접속자 목록은 확인할 수 없습니다.")

    # --- MOTD ---
    설명 = _평탄화(stats.get("description"))
    설명 = re.sub(r"[§&][0-9a-fk-orA-FK-OR]", "", 설명).strip()   # 마크 색상 코드 제거
    if 설명:
        한줄 = " ".join(설명.split())
        줄.append(f"MOTD: {한줄[:100]}")

    # --- 응답 속도 ---
    지연 = stats.get("time")
    if isinstance(지연, (int, float)):
        줄.append(f"응답 속도: {지연:.0f}ms")

    return "\n".join(줄)


# ============================================================
# 11. AI 장기 기억 (저장 / 조회 / 삭제)
# ============================================================

기억저장_선언 = types.FunctionDeclaration(
    name="remember",
    description=(
        "유저에 대해 앞으로도 계속 기억해야 할 정보를 장기 기억에 저장한다. "
        "유저가 '기억해둬'라고 말했을 때는 물론, 대화 중에 오래 기억할 가치가 있는 "
        "정보가 나오면 스스로 판단해서 저장한다. "
        "예: 좋아하는 것, 싫어하는 것, 직업이나 학년, 진행 중인 프로젝트, "
        "호칭 선호, 반복해서 언급하는 관심사, 중요한 약속이나 일정. "
        "반대로 한 번 쓰고 버릴 정보(오늘 날씨, 방금 계산한 값 등)는 저장하지 않는다. "
        "이미 비슷한 내용이 저장돼 있으면 중복 저장하지 말고, "
        "내용이 달라졌다면 먼저 forget 으로 옛 기억을 지운 뒤 새로 저장한다. "
        "한 문장으로 간결하게 적는다. "
        "기억마다 짧은 라벨을 붙인다 — 매 메시지에 라벨 목록만 보이고 내용은 recall 로 열어야 보이니, "
        "라벨만 보고도 무슨 기억인지 알 수 있게 쓴다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "기억할 내용. 한 문장. 예: '마인크래프트 서버를 운영한다'",
            },
            "label": {
                "type": "string",
                "description": f"내용을 한눈에 알 수 있는 짧은 라벨({최대_라벨_길이}자 이하). 예: '운영하는 서버', '좋아하는 음식', '시험 일정'",
            },
        },
        "required": ["content", "label"],
    },
)


기억불러오기_선언 = types.FunctionDeclaration(
    name="recall",
    description=(
        "이 유저에 대해 저장해 둔 장기 기억의 내용을 연다. "
        "시스템 프롬프트의 '장기 기억 라벨' 목록에서 지금 대화에 쓸모 있어 보이는 라벨이 있으면 "
        "그 번호를 memory_ids 로 넘겨 내용을 확인한 뒤 답한다 (라벨만 보고 내용을 지어내지 않는다). "
        "유저가 '나에 대해 뭘 알아?', '내가 뭐 기억해달라고 했지?' 라고 물으면 memory_ids 없이 불러 전부 본다. "
        "각 기억의 번호로 forget 을 호출할 수 있다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "열어 볼 기억 번호들 (라벨 목록의 #번호). 비우면 전부",
            }
        },
    },
)


기억삭제_선언 = types.FunctionDeclaration(
    name="forget",
    description=(
        "장기 기억에서 특정 항목을 지운다. "
        "유저가 '그건 잊어줘'라고 하거나, 기억이 더 이상 사실이 아니게 됐을 때 사용한다. "
        "지우려면 먼저 recall 로 번호를 확인해야 한다. "
        "저장 공간이 가득 찼다는 안내를 받으면 오래되고 덜 중요한 기억을 골라 지운다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "recall 로 확인한 기억의 번호",
            }
        },
        "required": ["memory_id"],
    },
)


def _라벨(label, content):
    """라벨이 없는 옛 기억은 내용 앞부분을 라벨로"""
    라벨 = " ".join((label or "").split())
    if not 라벨:
        라벨 = " ".join((content or "").split())
        라벨 = 라벨 if len(라벨) <= 최대_라벨_길이 else 라벨[:최대_라벨_길이 - 1] + "…"
    return 라벨[:최대_라벨_길이]


def 기억저장(user_id, content, label=None):
    내용 = (content or "").strip()
    if not 내용:
        return "저장할 내용이 비어 있습니다."
    내용 = 내용[:최대_기억_길이]
    라벨 = _라벨(label, 내용)

    cursor.execute("SELECT id, content FROM ai_memory WHERE user_id = ?", (user_id,))
    기존 = cursor.fetchall()

    for _id, 기존내용 in 기존:
        if 기존내용.strip() == 내용:
            return f"이미 같은 내용이 저장되어 있습니다. (#{_id})"

    if len(기존) >= 최대_기억_개수:
        목록 = "\n".join(f"#{i}: {c}" for i, c in 기존)
        return (
            f"기억 저장 공간이 가득 찼습니다 ({최대_기억_개수}개). "
            f"forget 으로 필요 없는 기억을 먼저 지워주세요.\n현재 기억:\n{목록}"
        )

    cursor.execute(
        "INSERT INTO ai_memory (user_id, content, label) VALUES (?, ?, ?)", (user_id, 내용, 라벨)
    )
    conn.commit()
    새_id = cursor.lastrowid
    return f"기억했습니다. (#{새_id} [{라벨}]: {내용}) — 현재 {len(기존) + 1}/{최대_기억_개수}개"


def 기억불러오기(user_id, memory_ids=None):
    cursor.execute(
        "SELECT id, content, created_at, label FROM ai_memory WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return "이 유저에 대해 저장된 기억이 없습니다."

    전체 = len(rows)
    없는 = []
    if memory_ids:
        try:
            고른 = {int(x) for x in (memory_ids if isinstance(memory_ids, (list, tuple)) else [memory_ids])}
        except (ValueError, TypeError):
            return "기억 번호는 숫자여야 합니다."
        있는 = {r[0] for r in rows}
        없는 = sorted(고른 - 있는)
        rows = [r for r in rows if r[0] in 고른]
    줄 = [f"#{i} [{_라벨(l, c)}]: {c}  (저장: {(t or '')[:10]})" for i, c, t, l in rows]
    끝 = f"\n없는 번호: {', '.join(f'#{x}' for x in 없는)}" if 없는 else ""
    if memory_ids:
        return (f"고른 기억 {len(rows)}개:\n" + "\n".join(줄) if 줄 else "고른 번호의 기억이 없습니다.") + 끝
    return f"저장된 기억 {전체}/{최대_기억_개수}개:\n" + "\n".join(줄)


def 기억삭제(user_id, memory_id):
    try:
        memory_id = int(memory_id)
    except (ValueError, TypeError):
        return "기억 번호는 숫자여야 합니다."

    cursor.execute(
        "SELECT content FROM ai_memory WHERE id = ? AND user_id = ?",
        (memory_id, user_id),
    )
    row = cursor.fetchone()
    if row is None:
        return f"#{memory_id} 번 기억을 찾을 수 없습니다. recall 로 번호를 다시 확인해주세요."

    cursor.execute(
        "DELETE FROM ai_memory WHERE id = ? AND user_id = ?", (memory_id, user_id)
    )
    conn.commit()
    return f"#{memory_id} 기억을 지웠습니다. ({row[0]})"


def 기억_전체삭제(user_id):
    """봇에서 !기억삭제 전체 같은 명령어로 쓸 수 있는 보조 함수 (AI 툴 아님)."""
    cursor.execute("DELETE FROM ai_memory WHERE user_id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount


def 기억_라벨목록(user_id):
    """봇이 매 메시지 시스템 프롬프트에 넣는 라벨 목록 (내용은 안 넣는다 — AI 가 recall 로 연다). 없으면 ''"""
    cursor.execute("SELECT id, content, label FROM ai_memory WHERE user_id = ? ORDER BY id", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        return ""
    return ", ".join(f"#{i} {_라벨(l, c)}" for i, c, l in rows)


def 기억_요약(user_id):
    """시스템 프롬프트에 끼워 넣을 짧은 요약. 봇에서 호출한다."""
    cursor.execute(
        "SELECT content FROM ai_memory WHERE user_id = ? ORDER BY id", (user_id,)
    )
    rows = cursor.fetchall()
    if not rows:
        return ""
    return " / ".join(r[0] for r in rows)


# ============================================================
# 12. 대출 (조회 / 실행 / 상환)
# ============================================================

def _대출_조회(user_id):
    cursor.execute(
        "SELECT principal, interest, due_at FROM loans WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return int(float(row[0])), int(float(row[1])), datetime.fromisoformat(row[2])


def _대출_차단여부(user_id):
    cursor.execute("SELECT reason FROM loan_banned WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def _남은시간_표기(기한):
    남음 = 기한 - datetime.now()
    if 남음.total_seconds() <= 0:
        return "기한 초과"
    일 = 남음.days
    시 = 남음.seconds // 3600
    분 = (남음.seconds % 3600) // 60
    if 일 > 0:
        return f"{일}일 {시}시간"
    if 시 > 0:
        return f"{시}시간 {분}분"
    return f"{분}분"


대출현황_선언 = types.FunctionDeclaration(
    name="get_my_loan",
    description=(
        "현재 대화 중인 유저 본인의 대출 현황을 조회한다. "
        "남은 원금과 이자, 갚아야 할 총액, 남은 기한, 10분당 붙는 이자를 알려준다. "
        "'내 빚 얼마야?', '대출 얼마 남았어?', '언제까지 갚아야 해?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 대출현황(user_id):
    대출 = _대출_조회(user_id)
    차단사유 = _대출_차단여부(user_id)

    if 대출 is None:
        답 = "현재 대출이 없습니다."
        if 차단사유:
            답 += f" (대출 차단 상태 — 사유: {차단사유})"
        else:
            답 += f" 최대 {최대_대출금:,}원까지 빌릴 수 있습니다."
        return 답

    원금, 이자, 기한 = 대출
    현금 = _get_money(user_id) or 0
    총액 = 원금 + 이자

    return (
        f"원금 {원금:,}원 + 이자 {이자:,}원 = 총 {총액:,}원을 갚아야 합니다.\n"
        f"남은 기한: {_남은시간_표기(기한)} ({기한.strftime('%m/%d %H:%M')}까지)\n"
        f"10분마다 {int(원금 * 대출_이자율):,}원씩 이자가 붙습니다.\n"
        f"현재 보유 현금: {현금:,}원"
    )


대출하기_선언 = types.FunctionDeclaration(
    name="take_loan",
    description=(
        "현재 대화 중인 유저 본인 명의로 돈을 빌린다. "
        "유저가 명확하게 금액을 말하며 빌려달라고 요청했을 때만 사용한다. "
        f"최대 {최대_대출금:,}원까지 가능하며, 10분마다 원금의 {int(대출_이자율 * 100)}%씩 "
        f"이자가 붙고 {대출_기한_일}일 안에 갚아야 한다. "
        "기한을 넘기면 현금과 주식이 모두 압류되고 대출이 영구 차단되므로, "
        "호출하기 전에 이 조건을 유저에게 알려주고 큰 금액일수록 신중하게 권한다. "
        "이미 대출이 있으면 추가로 빌릴 수 없다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "amount": {
                "type": "integer",
                "description": "빌릴 금액 (원). 유저가 직접 말한 금액만 넣는다.",
            }
        },
        "required": ["amount"],
    },
)


def 대출하기(user_id, amount):
    차단사유 = _대출_차단여부(user_id)
    if 차단사유:
        return f"대출이 차단된 상태입니다. 사유: {차단사유}"

    if _대출_조회(user_id):
        return "이미 대출이 있습니다. 먼저 상환해야 다시 빌릴 수 있습니다."

    try:
        amount = int(amount)
    except (ValueError, TypeError):
        return "금액은 숫자여야 합니다."

    if amount <= 0:
        return "1원 이상만 빌릴 수 있습니다."
    if amount > 최대_대출금:
        return f"최대 {최대_대출금:,}원까지만 빌릴 수 있습니다."

    지금 = datetime.now()
    기한 = 지금 + timedelta(days=대출_기한_일)

    현금 = _get_money(user_id)
    if 현금 is None:
        return "아직 등록되지 않은 유저입니다. !출석 을 먼저 사용해주세요."

    cursor.execute(
        "INSERT OR IGNORE INTO loans (user_id, principal, interest, borrowed_at, due_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, str(amount), "0", 지금.isoformat(), 기한.isoformat()),
    )
    if cursor.rowcount == 0:                 # 그 사이 다른 곳에서 대출을 받았다
        conn.rollback()
        return "이미 대출이 있습니다. 먼저 상환해야 다시 빌릴 수 있습니다."
    돈변경(user_id, amount)
    새잔액 = _get_money(user_id) or 0

    return (
        f"{amount:,}원을 대출했습니다. 현재 잔액 {새잔액:,}원.\n"
        f"10분마다 {int(amount * 대출_이자율):,}원씩 이자가 붙습니다.\n"
        f"상환 기한은 {기한.strftime('%Y-%m-%d %H:%M')} ({대출_기한_일}일 후)입니다."
    )


대출상환_선언 = types.FunctionDeclaration(
    name="repay_loan",
    description=(
        "현재 대화 중인 유저 본인의 대출을 상환한다. "
        "금액을 지정하면 그만큼, 생략하면 갚을 수 있는 만큼 최대로 갚는다. "
        "이자가 먼저 차감되고 남은 금액이 원금에서 빠진다. "
        "'빚 갚아줘', '100만원만 갚을래', '전액 상환' 같은 요청에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "amount": {
                "type": "integer",
                "description": "상환할 금액 (원). 생략하면 가능한 최대 금액을 상환한다.",
            }
        },
    },
)


def 대출상환(user_id, amount=None):
    대출 = _대출_조회(user_id)
    if 대출 is None:
        return "상환할 대출이 없습니다."

    원금, 이자, 기한 = 대출
    총액 = 원금 + 이자

    현금 = _get_money(user_id)
    if 현금 is None:
        return "아직 등록되지 않은 유저입니다."

    if amount is None:
        갚을돈 = min(총액, 현금)
        if 갚을돈 <= 0:
            return f"보유 현금이 없어 상환할 수 없습니다. (갚아야 할 금액: {총액:,}원)"
    else:
        try:
            갚을돈 = int(amount)
        except (ValueError, TypeError):
            return "금액은 숫자여야 합니다."
        if 갚을돈 <= 0:
            return "1원 이상만 상환할 수 있습니다."
        if 갚을돈 > 현금:
            return f"보유 현금이 부족합니다. (보유: {현금:,}원)"
        갚을돈 = min(갚을돈, 총액)

    남은이자 = max(0, 이자 - 갚을돈)
    이자상환 = 이자 - 남은이자
    남은원금 = max(0, 원금 - (갚을돈 - 이자상환))

    if not 돈변경(user_id, -갚을돈, 커밋=False):
        return "보유 현금이 부족합니다."
    현금 = 갚을돈 + (_get_money(user_id) or 0)

    if 남은원금 <= 0 and 남은이자 <= 0:
        cursor.execute("DELETE FROM loans WHERE user_id = ?", (user_id,))
        conn.commit()
        return (
            f"{갚을돈:,}원을 상환해 대출을 모두 갚았습니다! "
            f"남은 잔액 {현금 - 갚을돈:,}원"
        )

    cursor.execute(
        "UPDATE loans SET principal = ?, interest = ? WHERE user_id = ?",
        (str(남은원금), str(남은이자), user_id),
    )
    conn.commit()

    return (
        f"{갚을돈:,}원을 상환했습니다. "
        f"남은 빚은 원금 {남은원금:,}원 + 이자 {남은이자:,}원 = {남은원금 + 남은이자:,}원입니다. "
        f"남은 기한 {_남은시간_표기(기한)}. 현재 잔액 {현금 - 갚을돈:,}원"
    )


# ============================================================
# 13. 채굴 (현황 / 상점 / 구매 / 판매 / 전환 / 납부)
# ============================================================

_전부표현 = {"전부", "전체", "다", "모두", "max", "all", "MAX", "ALL"}


def _채굴기_목록():
    """반환: {코드: (이름, 가격, 10분당사토시, 설명, 소비전력W)}"""
    cursor.execute(
        "SELECT code, name, price, hashrate, description, power_w FROM mining_rigs "
        "ORDER BY CAST(price AS INTEGER)"
    )
    return {
        코드: (이름, int(float(가격)), int(float(해시)), 설명 or "", int(float(와트 or 0)))
        for 코드, 이름, 가격, 해시, 설명, 와트 in cursor.fetchall()
    }


def _보유장비(user_id):
    cursor.execute(
        "SELECT rig_code, quantity FROM user_rigs WHERE user_id = ? AND quantity > 0",
        (user_id,),
    )
    return {코드: 수량 for 코드, 수량 in cursor.fetchall()}


def _채굴정보(user_id):
    """반환: (사토시, 누적채굴, 정전복구시각, 누적Wh, 미납, 마지막청구)"""
    cursor.execute(
        "SELECT satoshi, total_mined, blackout_until, pending_wh, unpaid, last_billed "
        "FROM mining_balance WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return 0, 0, None, 0, 0, None

    def _t(값):
        if not 값:
            return None
        try:
            return datetime.fromisoformat(값)
        except ValueError:
            return None

    복구 = _t(row[2])
    if 복구 and 복구 <= datetime.now():
        복구 = None
    return (
        int(float(row[0] or 0)),
        int(float(row[1] or 0)),
        복구,
        int(float(row[3] or 0)),
        int(float(row[4] or 0)),
        _t(row[5]),
    )


def _채굴갱신(user_id, **값):
    cursor.execute(
        "INSERT OR IGNORE INTO mining_balance (user_id) VALUES (?)", (user_id,)
    )
    허용 = {"satoshi", "total_mined", "blackout_until", "pending_wh", "unpaid", "last_billed"}
    for 컬럼, v in 값.items():
        if 컬럼 in 허용:
            cursor.execute(
                f"UPDATE mining_balance SET {컬럼} = ? WHERE user_id = ?",
                (v if v is None else str(v), user_id),
            )


def _요금계산(wh):
    return int(wh / 1000 * 전기요금_kwh)


def _전환코인():
    cursor.execute(
        "SELECT symbol, name, price, satoshi_cost FROM stocks "
        "WHERE is_premium = 2 AND CAST(satoshi_cost AS INTEGER) > 0"
    )
    return [
        (코드, 이름, int(float(가격)), int(float(비용)))
        for 코드, 이름, 가격, 비용 in cursor.fetchall()
    ]


# ---------- 채굴 현황 ----------

채굴현황_선언 = types.FunctionDeclaration(
    name="get_mining_status",
    description=(
        "현재 대화 중인 유저 본인의 채굴 현황을 조회한다. "
        "보유 채굴기, 채굴 속도, 쌓인 사토시, 전기 요금, 정전이나 미납 여부를 알려준다. "
        "'내 채굴장 어때?', '사토시 얼마나 모았어?', '채굴 잘 되고 있어?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 채굴현황(user_id):
    장비 = _보유장비(user_id)
    목록 = _채굴기_목록()
    사토시, 누적, 복구시각, 누적wh, 미납, 마지막청구 = _채굴정보(user_id)

    if not 장비 and 사토시 == 0 and 미납 == 0:
        return "보유한 채굴기가 없습니다. 채굴기를 사면 사토시를 모을 수 있습니다."

    줄 = []
    총해시 = 총와트 = 0
    for 코드, 수량 in 장비.items():
        if 코드 not in 목록:
            continue
        이름, _, 해시, _, 와트 = 목록[코드]
        총해시 += 해시 * 수량
        총와트 += 와트 * 수량
        줄.append(f"{이름}({코드}) {수량}대")

    결과 = []
    if 줄:
        결과.append("보유 장비: " + ", ".join(줄) + f" (총 {sum(장비.values())}/{최대_채굴기}대)")
    else:
        결과.append("보유 장비: 없음")

    if 미납 > 0:
        결과.append(f"⚠️ 전기 요금 {미납:,}원이 미납되어 채굴이 중단된 상태입니다.")
    elif 복구시각:
        남은분 = int((복구시각 - datetime.now()).total_seconds() // 60) + 1
        결과.append(f"⚠️ 현재 정전으로 채굴이 멈췄습니다. 약 {남은분}분 후 복구됩니다.")
    else:
        결과.append(f"채굴 속도: 10분당 {총해시:,} sat (하루 {총해시 * 144:,} sat)")

    결과.append(f"보유 사토시: {사토시:,} sat (누적 채굴 {누적:,} sat)")

    if 총와트 > 0 or 누적wh > 0:
        일전기 = int(총와트 / 1000 * 24 * 전기요금_kwh)
        전기 = f"전기: 소비 {총와트:,}W, 하루 약 {일전기:,}원 / 이번 청구분 {_요금계산(누적wh):,}원"
        if 마지막청구:
            남은 = (마지막청구 + timedelta(hours=청구_주기_시간)) - datetime.now()
            if 남은.total_seconds() > 0:
                시간 = 남은.days * 24 + 남은.seconds // 3600
                전기 += f" (다음 청구까지 {시간}시간 {남은.seconds % 3600 // 60}분)"
        결과.append(전기)

    코인들 = _전환코인()
    if 코인들 and 사토시 > 0:
        가능 = [f"{n}({c}) {사토시 // b:,}개" for c, n, p, b in 코인들 if 사토시 >= b]
        if 가능:
            결과.append("지금 전환 가능: " + ", ".join(가능))

    return "\n".join(결과)


# ---------- 채굴기 상점 ----------

채굴기상점_선언 = types.FunctionDeclaration(
    name="list_mining_rigs",
    description=(
        "구매할 수 있는 채굴기 종류와 가격, 채굴 속도, 소비전력, 예상 수익을 보여준다. "
        "'채굴기 뭐 있어?', '어떤 채굴기 사는 게 좋아?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 채굴기상점():
    목록 = _채굴기_목록()
    if not 목록:
        return "등록된 채굴기가 없습니다."

    줄 = []
    for 코드, (이름, 가격, 해시, 설명, 와트) in 목록.items():
        일채굴 = 해시 * 144
        일전기 = int(와트 / 1000 * 24 * 전기요금_kwh)
        순익 = 일채굴 - 일전기
        회수 = f"{가격 / 순익:.1f}일" if 순익 > 0 else "회수 불가"
        줄.append(
            f"{이름}({코드}) — {가격:,}원 | 하루 {일채굴:,} sat 채굴, "
            f"전기세 {일전기:,}원 | 순익 {순익:,} | 본전 {회수}"
        )
    return (
        f"채굴기 목록 (1인 최대 {최대_채굴기}대, 전기요금 {전기요금_kwh:,}원/kWh):\n"
        + "\n".join(줄)
    )


# ---------- 채굴기 구매 ----------

채굴기구매_선언 = types.FunctionDeclaration(
    name="buy_mining_rig",
    description=(
        "현재 대화 중인 유저 본인이 채굴기를 구매한다. "
        "유저가 채굴기 종류와 수량을 말하며 사달라고 했을 때 사용한다. "
        f"1인당 최대 {최대_채굴기}대까지만 보유할 수 있고, "
        "채굴기는 매일 전기 요금이 나가므로 구매 전에 비용을 알려주는 것이 좋다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rig_code": {
                "type": "string",
                "description": "채굴기 코드 (예: CPU, GPU, ASIC, FARM, MEGA)",
            },
            "amount": {
                "type": "string",
                "description": (
                    "구매할 대수. 숫자를 넣거나 '전부' 를 넣으면 "
                    "잔액과 보유 한도 안에서 최대로 구매한다. 생략하면 1대."
                ),
            },
        },
        "required": ["rig_code"],
    },
)


def 채굴기구매(user_id, rig_code, amount=None):
    목록 = _채굴기_목록()
    코드 = (rig_code or "").upper().strip()

    if 코드 not in 목록:
        return f"'{rig_code}' 는 없는 채굴기입니다. 구매 가능한 코드: {', '.join(목록)}"

    이름, 가격, 해시, _, 와트 = 목록[코드]

    현재대수 = sum(_보유장비(user_id).values())
    여유대수 = 최대_채굴기 - 현재대수

    잔액 = _get_money(user_id)
    if 잔액 is None:
        return "아직 등록되지 않은 유저입니다."

    전부요청 = str(amount).strip() in _전부표현 if amount is not None else False

    if 전부요청:
        개수 = min(여유대수, 잔액 // 가격)
        if 개수 <= 0:
            if 여유대수 <= 0:
                return f"이미 최대 {최대_채굴기}대를 보유 중이라 더 살 수 없습니다."
            return f"{이름} 1대에 {가격:,}원인데 잔액이 {잔액:,}원뿐입니다."
    else:
        try:
            개수 = int(amount) if amount else 1
        except (ValueError, TypeError):
            return "수량은 숫자이거나 '전부' 여야 합니다."
        if 개수 <= 0:
            return "1대 이상만 구매할 수 있습니다."
        if 개수 > 여유대수:
            return (
                f"최대 {최대_채굴기}대까지만 보유할 수 있습니다. "
                f"현재 {현재대수}대, 추가 가능 {여유대수}대"
            )

    총액 = 가격 * 개수
    if 잔액 < 총액:
        return f"잔액이 부족합니다. 필요 {총액:,}원 / 보유 {잔액:,}원"

    if not 돈변경(user_id, -총액, 커밋=False):
        return f"잔액이 부족합니다. 필요 {총액:,}원"
    cursor.execute(
        "INSERT INTO user_rigs (user_id, rig_code, quantity) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, rig_code) DO UPDATE SET quantity = quantity + ?",
        (user_id, 코드, 개수, 개수),
    )
    conn.commit()

    장비 = _보유장비(user_id)
    총해시 = sum(목록[c][2] * q for c, q in 장비.items() if c in 목록)
    총와트 = sum(목록[c][4] * q for c, q in 장비.items() if c in 목록)

    return (
        f"{이름}({코드}) {개수}대를 {총액:,}원에 구매했습니다. 남은 잔액 {잔액 - 총액:,}원.\n"
        f"이제 10분당 {총해시:,} sat을 채굴하며, "
        f"전기세는 하루 약 {int(총와트 / 1000 * 24 * 전기요금_kwh):,}원입니다."
    )


# ---------- 채굴기 판매 ----------

채굴기판매_선언 = types.FunctionDeclaration(
    name="sell_mining_rig",
    description=(
        "현재 대화 중인 유저 본인이 보유한 채굴기를 판매한다. "
        f"구매가의 {int(채굴기_환급률 * 100)}%를 돌려받는다. "
        "수량을 생략하면 해당 채굴기를 전부 판매한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rig_code": {"type": "string", "description": "판매할 채굴기 코드"},
            "amount": {
                "type": "string",
                "description": "판매할 대수. 숫자 또는 '전부'. 생략하면 보유한 전량을 판매한다.",
            },
        },
        "required": ["rig_code"],
    },
)


def 채굴기판매(user_id, rig_code, amount=None):
    목록 = _채굴기_목록()
    장비 = _보유장비(user_id)
    코드 = (rig_code or "").upper().strip()

    if 코드 not in 목록:
        return f"'{rig_code}' 는 없는 채굴기입니다."

    보유수량 = 장비.get(코드, 0)
    if 보유수량 <= 0:
        return f"{코드} 채굴기를 보유하고 있지 않습니다."

    if amount is None or str(amount).strip() in _전부표현:
        개수 = 보유수량
    else:
        try:
            개수 = int(amount)
        except (ValueError, TypeError):
            return "수량은 숫자이거나 '전부' 여야 합니다."
        if 개수 <= 0:
            return "1대 이상만 판매할 수 있습니다."
        if 개수 > 보유수량:
            return f"보유 수량보다 많이 팔 수 없습니다. (보유 {보유수량}대)"

    이름, 가격 = 목록[코드][0], 목록[코드][1]
    환급액 = int(가격 * 채굴기_환급률) * 개수
    남은수량 = 보유수량 - 개수

    cursor.execute(
        "UPDATE user_rigs SET quantity = quantity - ? WHERE user_id = ? AND rig_code = ? AND quantity >= ?",
        (개수, user_id, 코드, 개수),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        return "그 사이 보유 수량이 바뀌었습니다. 다시 시도해주세요."
    cursor.execute("DELETE FROM user_rigs WHERE user_id = ? AND quantity <= 0", (user_id,))
    돈변경(user_id, 환급액)
    잔액 = (_get_money(user_id) or 0) - 환급액

    return (
        f"{이름}({코드}) {개수}대를 판매해 {환급액:,}원을 받았습니다. "
        f"남은 수량 {남은수량}대, 현재 잔액 {잔액 + 환급액:,}원"
    )


# ---------- 전환 가능 코인 ----------

전환목록_선언 = types.FunctionDeclaration(
    name="list_coin_conversions",
    description=(
        "채굴한 사토시를 어떤 코인으로 바꿀 수 있는지, "
        "코인 1개당 사토시가 얼마나 드는지 보여준다. "
        "'사토시로 뭘 살 수 있어?', '코인 전환 얼마야?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 전환목록(user_id):
    코인들 = _전환코인()
    if not 코인들:
        return "전환할 수 있는 코인이 없습니다."

    사토시 = _채굴정보(user_id)[0]
    줄 = [
        f"{이름}({코드}) — 1개당 {비용:,} sat, 시세 {시세:,}원 "
        f"(지금 {사토시 // 비용:,}개 가능)"
        for 코드, 이름, 시세, 비용 in 코인들
    ]
    return f"보유 사토시 {사토시:,} sat\n전환 가능한 코인:\n" + "\n".join(줄)


# ---------- 사토시 전환 ----------

사토시전환_선언 = types.FunctionDeclaration(
    name="convert_satoshi",
    description=(
        "현재 대화 중인 유저 본인이 채굴한 사토시를 코인으로 전환한다. "
        "전환한 코인은 포트폴리오에 들어가며 매도로 현금화할 수 있다. "
        "수량을 생략하면 가능한 만큼 전부 전환한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "coin_code": {"type": "string", "description": "전환할 코인 코드 (예: BTC)"},
            "amount": {"type": "integer", "description": "전환할 개수. 생략하면 최대"},
        },
        "required": ["coin_code"],
    },
)


def 사토시전환(user_id, coin_code, amount=None):
    코인들 = _전환코인()
    if not 코인들:
        return "전환할 수 있는 코인이 없습니다."

    코드 = (coin_code or "").upper().strip()
    대상 = next((c for c in 코인들 if c[0] == 코드), None)
    if 대상 is None:
        return f"'{coin_code}' 는 전환할 수 없습니다. 가능한 코인: {', '.join(c[0] for c in 코인들)}"

    _, 코인이름, 시세, 비용 = 대상
    사토시, 누적, _, _, _, _ = _채굴정보(user_id)
    최대가능 = 사토시 // 비용

    if 최대가능 <= 0:
        return f"사토시가 부족합니다. {코인이름} 1개에 {비용:,} sat 필요 (보유 {사토시:,} sat)"

    try:
        개수 = int(amount) if amount else 최대가능
    except (ValueError, TypeError):
        return "수량은 숫자여야 합니다."
    if 개수 <= 0:
        return "1개 이상만 전환할 수 있습니다."
    if 개수 > 최대가능:
        return (
            f"사토시가 부족합니다. 필요 {비용 * 개수:,} sat / "
            f"보유 {사토시:,} sat (최대 {최대가능:,}개)"
        )

    사용 = 비용 * 개수
    if not 사토시변경(user_id, -사용, 커밋=False):
        return "사토시가 부족합니다."
    _보유추가(user_id, 코드, 개수, 시세 * 개수)
    conn.commit()

    return (
        f"{사용:,} sat을 {코인이름} {개수:,}개로 전환했습니다 (평가액 {시세 * 개수:,}원). "
        f"남은 사토시 {사토시 - 사용:,} sat"
    )


# ---------- 전기 요금 납부 ----------

전기납부_선언 = types.FunctionDeclaration(
    name="pay_electricity",
    description=(
        "현재 대화 중인 유저 본인의 미납된 전기 요금을 납부한다. "
        "미납 상태에서는 채굴이 중단되므로, 미납이 있다면 납부를 권한다. "
        "'전기세 내줘', '미납금 갚아줘' 같은 요청에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 전기납부(user_id):
    _, _, _, 누적wh, 미납, _ = _채굴정보(user_id)

    if 미납 <= 0:
        return (
            f"미납된 전기 요금이 없습니다. "
            f"현재까지 사용량 {누적wh / 1000:.1f} kWh (예상 청구액 {_요금계산(누적wh):,}원)"
        )

    잔액 = _get_money(user_id)
    if 잔액 is None or 잔액 <= 0:
        return f"잔액이 없어 미납금 {미납:,}원을 납부할 수 없습니다."

    납부액 = min(잔액, 미납)
    if not 돈변경(user_id, -납부액, 커밋=False):
        return "잔액이 부족합니다. 다시 시도해주세요."
    cursor.execute(
        "UPDATE mining_balance SET unpaid = CAST(MAX(0, CAST(unpaid AS INTEGER) - ?) AS TEXT) WHERE user_id = ?",
        (납부액, user_id),
    )
    conn.commit()

    if 미납 - 납부액 <= 0:
        return (
            f"미납금 {납부액:,}원을 모두 납부했습니다. 채굴이 재개됩니다. "
            f"남은 잔액 {잔액 - 납부액:,}원"
        )
    return (
        f"{납부액:,}원을 납부했지만 아직 {미납 - 납부액:,}원이 남아 "
        f"채굴이 중단된 상태입니다."
    )


# ============================================================
# 14. 전량 매도 / 최대 매수
# ============================================================

전량매도_선언 = types.FunctionDeclaration(
    name="sell_all_stocks",
    description=(
        "현재 대화 중인 유저 본인이 보유한 주식과 코인을 한 번에 전부 매도한다. "
        "'주식 다 팔아줘', '전량 매도', '가진 거 전부 현금화해줘' 같은 요청에 사용한다. "
        "특정 종목만 팔 때는 sell_stock 을 쓴다. 종목마다 따로 호출할 필요가 없다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 전량매도(user_id):
    cursor.execute(
        "SELECT symbol FROM portfolio WHERE user_id = ? AND CAST(quantity AS INTEGER) > 0",
        (user_id,),
    )
    종목들 = [r[0] for r in cursor.fetchall()]
    if not 종목들:
        return "보유 중인 주식이 없습니다."

    막힘 = 주식.거래불가()
    if 막힘:
        # 장이 닫혀 있으면 코인만 판다
        cursor.execute("SELECT symbol FROM stocks WHERE is_premium = 2")
        코인들 = {r[0] for r in cursor.fetchall()}
        종목들 = [s for s in 종목들 if s in 코인들]
        if not 종목들:
            return 막힘

    줄 = []
    총수령 = 총손익 = 0
    현금 = _get_money(user_id) or 0
    for 심볼 in 종목들:
        성공, r = 매도실행(user_id, 심볼, None)
        if not 성공:
            줄.append(f"{심볼}: {r}")
            continue
        부호 = "+" if r["profit"] >= 0 else ""
        줄.append(f"{r['name']}({r['symbol']}) {r['qty']:,}주 @{r['fill']:,} → {r['income']:,}원 ({부호}{r['profit']:,})")
        총수령 += r["income"]
        총손익 += r["profit"]
        현금 = r["cash"]

    부호 = "+" if 총손익 >= 0 else ""
    return (
        ("장이 닫혀 있어 코인만 매도했습니다. 주식은 장중에 다시 요청해주세요.\n" if 막힘
         else "보유 종목을 전부 매도했습니다.\n") + "\n".join(줄)
        + f"\n합계 {총수령:,}원 (손익 {부호}{총손익:,}원), 현재 잔액 {현금:,}원"
    )


전량매수_선언 = types.FunctionDeclaration(
    name="buy_stock_max",
    description=(
        "현재 대화 중인 유저 본인의 잔액으로 특정 종목을 살 수 있는 만큼 최대로 매수한다. "
        "'삼성전자 풀매수', '가진 돈 전부 애플에 넣어줘', '사토시 전부 비트코인으로' 같은 "
        "요청에 사용한다. 주식은 현금, 코인은 사토시로 산다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드 또는 종목명"},
            "reserve": {"type": "integer", "description": "남겨둘 금액(원). 생략하면 잔액 전부 사용"},
        },
        "required": ["symbol"],
    },
)


def 전량매수(user_id, symbol, reserve=None):
    try:
        남길돈 = max(0, int(reserve)) if reserve else 0
    except (ValueError, TypeError):
        남길돈 = 0

    정보 = 시장정보(symbol or "")
    if 정보 is None:
        return f"'{symbol}'은(는) 존재하지 않는 종목입니다."

    if 정보["market"] == 2:
        예산, 단위 = _사토시잔액(user_id), "sat"
    else:
        막힘 = 주식.거래불가()
        if 막힘:
            return 막힘
        예산 = _get_money(user_id)
        if 예산 is None:
            return "아직 등록되지 않은 유저입니다."
        단위 = "원"

    가능 = None if 정보["market"] == 2 else 추가매수가능(정보, user_id)
    if 가능 == 0:
        return f"{정보['name']}은(는) 더 살 수 없습니다. (1인 최대 {지분한도(정보):,}주 또는 남은 물량 없음)"
    개수 = _최대매수수량(정보, max(0, 예산 - 남길돈), 가능)
    if 개수 <= 0:
        if 정보["remain"] == 0:
            return f"{정보['name']}은(는) 유통 물량이 모두 팔려 살 수 없습니다."
        return f"사용 가능한 {max(0, 예산 - 남길돈):,}{단위}로는 1개도 살 수 없습니다."

    return 주식매수(user_id, 정보["symbol"], 개수)


# ============================================================
# 15. 채굴기 전량 판매
# ============================================================

채굴기전량판매_선언 = types.FunctionDeclaration(
    name="sell_all_mining_rigs",
    description=(
        "현재 대화 중인 유저 본인이 보유한 모든 채굴기를 한 번에 전부 판매한다. "
        "'채굴기 다 팔아줘', '채굴장 정리해줘' 같은 요청에 사용한다. "
        "특정 채굴기만 팔 때는 sell_mining_rig 를 쓴다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 채굴기전량판매(user_id):
    장비 = _보유장비(user_id)
    목록 = _채굴기_목록()

    if not 장비:
        return "보유한 채굴기가 없습니다."

    줄 = []
    총환급 = 0
    for 코드, 수량 in 장비.items():
        if 코드 not in 목록:
            continue
        이름, 가격 = 목록[코드][0], 목록[코드][1]
        환급 = int(가격 * 채굴기_환급률) * 수량
        총환급 += 환급
        줄.append(f"{이름}({코드}) {수량}대 → {환급:,}원")

    if not 줄:
        return "판매할 채굴기가 없습니다."

    cursor.execute("DELETE FROM user_rigs WHERE user_id = ?", (user_id,))
    돈변경(user_id, 총환급)
    잔액 = (_get_money(user_id) or 0) - 총환급

    return (
        f"채굴기를 전부 판매했습니다.\n"
        + "\n".join(줄)
        + f"\n총 {총환급:,}원을 받아 현재 잔액은 {잔액 + 총환급:,}원입니다."
    )


# ============================================================
# 총자산 — !돈, !순위, 웹에서 쓴다
# ============================================================

골드_환율 = 10_000   # RPG.py 와 같게 (1골드 = 1만원)


def 총자산(user_id):
    """
    지금 전부 정리하면 남는 돈.
    현금 + 사토시(1 sat = 1원) + 주식·코인(팔면 받는 돈) + 채굴기(되팔 때 값) + 골드 − 대출
    """
    현금 = _get_money(user_id) or 0
    사토시 = _사토시잔액(user_id)

    주식액 = 0
    cursor.execute(
        "SELECT symbol, quantity FROM portfolio WHERE user_id = ? AND CAST(quantity AS INTEGER) > 0",
        (user_id,),
    )
    for 심볼, 수량 in cursor.fetchall():
        정보 = 시장정보(심볼)
        if 정보:
            주식액 += 청산가치(정보, int(float(수량)))[1]

    채굴기 = 0
    try:
        cursor.execute(
            "SELECT r.price, u.quantity FROM user_rigs u JOIN mining_rigs r ON r.code = u.rig_code "
            "WHERE u.user_id = ? AND u.quantity > 0",
            (user_id,),
        )
        채굴기 = sum(int(int(float(가격)) * 채굴기_환급률) * 수량 for 가격, 수량 in cursor.fetchall())
    except sqlite3.OperationalError:
        pass

    대출 = 0
    try:
        cursor.execute("SELECT principal, interest FROM loans WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            대출 = int(float(row[0] or 0)) + int(float(row[1] or 0))
    except sqlite3.OperationalError:
        pass

    골드 = 0
    try:
        cursor.execute("SELECT COALESCE(gold, 0) FROM rpg_characters WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            골드 = int(row[0]) * 골드_환율
    except sqlite3.OperationalError:
        pass

    레버 = 0
    try:
        레버 = 주식.평가합(user_id)
    except sqlite3.OperationalError:
        pass

    return {
        "현금": 현금, "사토시": 사토시, "주식": 주식액, "채굴기": 채굴기, "골드": 골드, "대출": 대출, "레버리지": 레버,
        "합계": 현금 + 사토시 + 주식액 + 채굴기 + 골드 + 레버 - 대출,
    }


# ============================================================
# 16. RPG — 봇 폴더의 RPG.py 를 그대로 쓴다 (던전 전투는 제외)
# ============================================================

def _rpg():
    import RPG
    return RPG


def _선언(이름, 설명, 속성=None, 필수=()):
    return types.FunctionDeclaration(
        name=이름, description=설명,
        parameters={"type": "object", "properties": 속성 or {}, **({"required": list(필수)} if 필수 else {})},
    )


RPG정보_선언 = _선언(
    "rpg_status",
    "현재 대화 중인 유저 본인의 RPG 캐릭터 정보를 본다. 레벨, 능력치, 공격력·체력·회피·치명타, "
    "착용 무기·갑옷, 골드, 스킬포인트, 최고 층, 펫, 간식을 알려준다. 'RPG 내 캐릭터 어때?' 같은 질문에 사용한다.",
)
RPG스탯_선언 = _선언(
    "rpg_allocate_stat", "RPG 스킬포인트를 능력치에 찍는다. 힘=공격력, 체력=최대체력, 속도=회피, 지능=치명타. 능력치 하나는 최대 25.",
    {"stat": {"type": "string", "description": "힘 / 체력 / 속도 / 지능"},
     "points": {"type": "string", "description": "찍을 포인트 수 또는 '전부'"}},
    ("stat", "points"),
)
RPG상점_선언 = _선언(
    "rpg_shop", "RPG 상점 목록을 본다. 가격, 성능, 잠금 여부(레벨·업적 조건)를 알려준다.",
    {"category": {"type": "string", "description": "무기 / 갑옷 / 아이템"}}, ("category",),
)
RPG구매_선언 = _선언(
    "rpg_buy", "RPG 상점에서 무기·갑옷·아이템을 골드로 산다. 아이템은 수량을 정할 수 있다.",
    {"name": {"type": "string", "description": "물건 이름 (앞부분만 써도 된다)"},
     "amount": {"type": "integer", "description": "아이템 수량. 무기·갑옷은 무시"}}, ("name",),
)
RPG판매_선언 = _선언(
    "rpg_sell", "RPG 물건을 판다. 무기·갑옷은 산 가격의 85%를 환불받고 강화 수치는 사라진다. 착용 중인 건 팔 수 없다.",
    {"name": {"type": "string", "description": "물건 이름"},
     "amount": {"type": "string", "description": "아이템 수량 또는 '전부'"}}, ("name",),
)
RPG장착_선언 = _선언(
    "rpg_equip", "가지고 있는 RPG 무기나 갑옷을 착용한다.",
    {"name": {"type": "string", "description": "무기 또는 갑옷 이름"}}, ("name",),
)
RPG강화_선언 = _선언(
    "rpg_enhance",
    "RPG 무기를 강화한다. target 을 주면 그 수치까지 자동으로 반복하고, 없으면 1번만 한다. "
    "성공 확률은 (100 - 현재 강화)%, 30강부터 실패 시 하락, 50강부터 소멸 위험이 있으니 "
    "50강 이상을 목표로 할 때는 유저에게 위험을 알린 뒤에 사용한다.",
    {"name": {"type": "string", "description": "무기 이름. 생략하면 착용 중인 무기"},
     "target": {"type": "integer", "description": "목표 강화 수치 (자동 강화)"}},
)
RPG환전_선언 = _선언(
    "rpg_gold_exchange", "현금과 RPG 골드를 바꾼다. 1골드 = 1만원.",
    {"direction": {"type": "string", "description": "buy(원→골드) 또는 sell(골드→원)"},
     "amount": {"type": "string", "description": "골드 수량 또는 '전부'"}}, ("direction", "amount"),
)
RPG펫_선언 = _선언(
    "rpg_pet",
    "RPG 펫을 관리한다. action: info(목록·능력), buy(데려오기), equip(장착), unequip(해제), "
    "upgrade(간식으로 체력 또는 능력 레벨업), weapon(펫에게 쥐어줄 무기 설정, 비우면 맨손).",
    {"action": {"type": "string", "description": "info / buy / equip / unequip / upgrade / weapon"},
     "name": {"type": "string", "description": "펫 이름(강아지·고양이·돼지) 또는 weapon 일 때 무기 이름"},
     "kind": {"type": "string", "description": "upgrade 일 때 '체력' 또는 '능력'"}}, ("action",),
)
RPG업적_선언 = _선언("rpg_achievements", "RPG 업적 목록과 달성 여부, 보상을 본다.")


def RPG정보(user_id):
    R = _rpg()
    c = R.캐릭터(user_id)
    if c is None:
        return "RPG 캐릭터가 없습니다. `!RPG 생성 [이름]` 으로 만들 수 있습니다."
    s = R.전투능력(c)
    펫들 = R.내펫들(user_id)
    장착펫 = next((p for p in 펫들 if p["장착"]), None)
    관리 = R.관리자인가(user_id)
    레벨문구 = "관리자 Lv.∞" if 관리 else f"Lv.{c['level']} (경험치 {c['exp']:,}/{R.필요경험치(c['level']):,})"
    return (
        f"{c['name']} · {레벨문구} · 최고 {c['best']}층\n"
        f"능력치: 힘 {c['힘']} 체력 {c['체력']} 속도 {c['속도']} 지능 {c['지능']} (최대 {R.능력치_최대}) · 스킬포인트 {c['points']}\n"
        f"공격력 {s['공격력']} (무기 포함 {s['무기피해']:.0f}) · 체력 {s['최대체력']} · 방어 {s['방어력']} · "
        f"회피 {s['회피'] * 100:.0f}% · 치명타 {s['치명'] * 100:.0f}%\n"
        f"무기 {c['weapon']}" + (f" +{s['강화']}" if s["강화"] else "") + f" · 갑옷 {c['armor']} · 골드 {c['gold']:,}\n"
        + (f"펫 {장착펫['이름']} (체력 Lv.{장착펫['체력lv']}, 능력 Lv.{장착펫['능력lv']}) · 펫 무기 {R.펫무기(user_id) or '맨손'} · "
           if 장착펫 else "펫 없음 · ")
        + f"간식 {'∞' if R.간식(user_id) >= R.관리자_무한 else R.간식(user_id)}"
    )


def RPG스탯(user_id, stat, points):
    ok, r = _rpg().포인트사용(user_id, stat, points)
    return r if not ok else f"{stat}에 포인트를 찍었습니다. 남은 스킬포인트 {r['points']}. 지금 {stat} {r[_rpg().능력치_별칭.get(str(stat).lower(), stat)]}"


def RPG상점(user_id, category):
    R = _rpg()
    cat = {"무기": "무기", "갑옷": "갑옷", "방어구": "갑옷"}.get(category, "아이템")
    줄 = []
    if cat in ("무기", "갑옷"):
        표, 종류 = (R.무기, "weapon") if cat == "무기" else (R.갑옷, "armor")
        for n, d in 표.items():
            if d.get("숨김") and not R.보유(user_id, 종류, n):
                continue
            됨, 이유 = R.해금여부(user_id, 종류, n)
            상태 = "보유" if R.보유(user_id, 종류, n) else (f"잠김({이유})" if not 됨 else f"{d['price']:,}골드" if d["price"] else "무료")
            수치 = f"공격 {d['atk']}" if 종류 == "weapon" else f"방어 {d['dfn']}"
            줄.append(f"{n}: {상태} · {수치}")
    else:
        for n, d in R.아이템.items():
            줄.append(f"{n}: {d['price']:,}골드 · {d['설명']}")
    return f"[{cat}] 보유 골드 {R.캐릭터(user_id)['gold']:,}\n" + "\n".join(줄)


def RPG구매(user_id, name, amount=1):
    ok, r = _rpg().구매(user_id, name, amount or 1)
    return r if not ok else f"{r['이름']}" + (f" {r['개수']}개" if r["종류"] == "item" else "") + f"를 {r['가격']:,}골드에 샀습니다. 남은 골드 {r['골드']:,}"


def RPG판매(user_id, name, amount=1):
    try:
        ok, r = _rpg().판매(user_id, name, amount or 1)
    except ValueError:
        return "수량이 올바르지 않습니다."
    return r if not ok else f"{r['이름']} {r['개수']}개를 팔아 {r['받은골드']:,}골드를 받았습니다. 보유 {r['골드']:,}"


def RPG장착(user_id, name):
    ok, r = _rpg().장착(user_id, name)
    return f"{r}을(를) 장착했습니다." if ok else r


def RPG강화(user_id, name=None, target=None):
    R = _rpg()
    if target:
        ok, r = R.자동강화(user_id, name, target)
        if not ok:
            return r
        이후 = "소멸" if r["이후"] is None else f"+{r['이후']}"
        return (f"{r['무기']} 자동 강화: +{r['시작']} → {이후} (목표 +{r['목표']}). {r['시도']}번 시도, "
                f"성공 {r['성공']} 실패 {r['실패']} 하락 {r['하락']}, 골드 {r['골드']:,} 사용. {r['결과']}")
    ok, r = R.강화(user_id, name)
    if not ok:
        return r
    return f"{r['무기']} 강화 {r['결과']}: +{r['이전']} → +{r['이후']} (성공률 {r['성공률'] * 100:.0f}%, 비용 {r['비용']:,}골드)"


def RPG환전(user_id, direction, amount):
    ok, r = _rpg().골드환전(user_id, "구매" if str(direction).lower() in ("buy", "구매") else "판매", amount)
    return r if not ok else f"{r['개수']:,}골드 ↔ {r['금액']:,}원 환전 완료. 골드 {r['골드']:,}, 현금 {r['현금']:,}원"


def RPG펫(user_id, action, name=None, kind=None):
    R = _rpg()
    a = str(action).lower()
    if a == "info":
        줄 = []
        가진 = {p["이름"]: p for p in R.내펫들(user_id)}
        for n, d in R.펫.items():
            p = 가진.get(n)
            if d.get("숨김") and not p:
                continue
            if p and n == "스탠드":
                st = R.내스탠드(user_id) or {"능력들": []}
                줄.append(f"스탠드 '{p['별명']}'{' (장착)' if p['장착'] else ''} Lv.{p['능력lv']} — 능력: "
                         + (", ".join(f"{a['이름']}({a.get('설명', '')})" for a in st["능력들"]) or "없음"))
                continue
            줄.append(f"{n}: " + (f"보유{' (장착)' if p['장착'] else ''} 체력 Lv.{p['체력lv']} 능력 Lv.{p['능력lv']} — {R.펫능력문구(n, p['능력lv'])}"
                                   if p else f"{d['price']:,}골드 — {d['설명']}"))
        return "\n".join(줄) + f"\n펫 무기: {R.펫무기(user_id) or '맨손'} · 간식 {R.간식(user_id)}"
    if a == "buy":
        ok, r = R.펫구매(user_id, name or "")
        return f"{r['이름']}을(를) {r['가격']:,}골드에 데려왔습니다." if ok else r
    if a in ("equip", "unequip"):
        ok, r = R.펫장착(user_id, None if a == "unequip" else name)
        return ("펫을 해제했습니다." if r is None else f"{r}을(를) 장착했습니다.") if ok else r
    if a == "upgrade":
        ok, r = R.펫강화(user_id, name or "", kind or "")
        return f"{r['이름']} {r['종류']} Lv.{r['레벨']} (간식 {r['비용']} 사용)" if ok else r
    if a == "weapon":
        ok, r = R.펫무기설정(user_id, name)
        return (f"펫이 {r}을(를) 듭니다." if r else "펫이 맨손으로 싸웁니다.") if ok else r
    return "action 은 info / buy / equip / unequip / upgrade / weapon 중 하나여야 합니다."


def RPG업적(user_id):
    R = _rpg()
    R.업적확인(user_id)
    return "\n".join(f"{'✅' if R.업적보유(user_id, n) else '⬜'} {n} — {d['설명']} ({R.업적보상문구(n)})" for n, d in R.업적.items())


# ============================================================
# 시스템 켜기/끄기 — 봇·웹·AI가 같은 설정을 본다
# ============================================================

cursor.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)")
conn.commit()

시스템목록 = {"경제": "경제", "economy": "경제", "rpg": "RPG", "RPG": "RPG", "알피지": "RPG"}
_시스템캐시 = {}


def 시스템켜짐(이름):
    """꺼져 있으면 False. 다른 프로그램이 바꿔도 3초 안에 반영된다."""
    이름 = 시스템목록.get(이름, 이름)
    저장 = _시스템캐시.get(이름)
    if 저장 and time.monotonic() - 저장[1] < 3:
        return 저장[0]
    cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (f"system:{이름}",))
    r = cursor.fetchone()
    켜짐 = not (r and r[0] == "off")
    _시스템캐시[이름] = (켜짐, time.monotonic())
    return 켜짐


def 시스템설정(이름, 켜짐):
    이름 = 시스템목록.get(이름, 이름)
    cursor.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
        (f"system:{이름}", "on" if 켜짐 else "off"),
    )
    conn.commit()
    _시스템캐시.pop(이름, None)


# ============================================================
# 멤버 역할 조회 (디스코드 캐시에서 읽는다)
# ============================================================

봇 = None   # on_ready 에서 tools.봇 = bot 으로 넣어준다


def _멤버들():
    """봇이 들어가 있는 서버의 멤버를 (서버, 멤버) 로 돌려준다. 같은 사람은 한 번만."""
    본 = set()
    for g in (_봇().guilds if _봇() else []):
        for m in g.members:
            if m.id not in 본:
                본.add(m.id)
                yield g, m


def _멤버찾기(이름):
    이름 = str(이름).strip().lstrip("@")
    숫자 = re.sub(r"\D", "", 이름)
    if 숫자 and len(숫자) >= 15:
        for g in _봇().guilds:
            m = g.get_member(int(숫자))
            if m:
                return [m]
    try:
        db_id, _ = _find_user_by_name(이름)
    except Exception:
        db_id = None
    if db_id and str(db_id).isdigit():
        for g in _봇().guilds:
            m = g.get_member(int(db_id))
            if m:
                return [m]
    소문자 = 이름.lower()
    후보들 = lambda m: [x for x in (m.display_name, m.name, m.global_name, getattr(m, "nick", None)) if x]
    정확 = [m for _, m in _멤버들() if any(x.lower() == 소문자 for x in 후보들(m))]
    if 정확:
        return 정확
    return [m for _, m in _멤버들() if any(소문자 in x.lower() for x in 후보들(m))]


def _역할목록(m):
    역할 = [r for r in m.roles if not r.is_default()]
    역할.sort(key=lambda r: r.position, reverse=True)
    return [r.name for r in 역할]


멤버역할_선언 = _선언(
    "get_member_roles",
    "디스코드 서버 멤버가 가진 역할을 조회한다. name 을 주면 그 사람의 역할을, role 을 주면 그 역할을 가진 멤버 목록을, "
    "둘 다 없으면 대화 중인 유저 본인의 역할을 알려준다. '누구 역할 뭐야?', '관리자 누구야?', '내 역할 뭐야?' 같은 질문에 사용한다.",
    {
        "name": {"type": "string", "description": "역할을 볼 멤버의 닉네임, 이름 또는 디스코드 ID"},
        "role": {"type": "string", "description": "이 역할을 가진 멤버 목록을 보고 싶을 때 역할 이름"},
    },
)


def 멤버역할(user_id, name=None, role=None):
    봇 = _봇()
    if 봇 is None or not 봇.guilds:
        return "봇이 아직 서버 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."

    if role:
        찾을 = str(role).strip().lstrip("@").lower()
        결과 = {}
        for g in 봇.guilds:
            for r in g.roles:
                if r.is_default():
                    continue
                if r.name.lower() == 찾을 or 찾을 in r.name.lower():
                    결과.setdefault(r.name, set()).update(m.display_name for m in r.members)
        if not 결과:
            return f"'{role}' 이름의 역할을 찾지 못했습니다."
        줄 = []
        for 이름, 멤버 in list(결과.items())[:5]:
            목록 = sorted(멤버)
            표시 = ", ".join(목록[:50]) + (f" 외 {len(목록) - 50}명" if len(목록) > 50 else "")
            줄.append(f"[{이름}] {len(목록)}명: {표시 or '없음'}")
        return "\n".join(줄)

    if name:
        찾음 = _멤버찾기(name)
        if not 찾음:
            return f"'{name}' 멤버를 서버에서 찾지 못했습니다."
        if len(찾음) > 5:
            return f"'{name}'에 해당하는 멤버가 {len(찾음)}명입니다. 더 정확한 이름을 알려주세요: " + ", ".join(m.display_name for m in 찾음[:10])
    else:
        찾음 = []
        for g in 봇.guilds:
            m = g.get_member(int(user_id))
            if m:
                찾음 = [m]
                break
        if not 찾음:
            return "대화 중인 유저를 서버에서 찾지 못했습니다."

    줄 = []
    for m in 찾음:
        역할 = _역할목록(m)
        줄.append(f"{m.display_name}({m.name}): " + (", ".join(역할) if 역할 else "역할 없음"))
    return "\n".join(줄)


# ============================================================
# 음악 (우리봇v2.py 음악 칸을 tools.메인 으로 쓴다 — 결과가 코루틴이라 on_message 에서 await 한다)
# ============================================================

메인 = None   # 봇이 켜질 때 우리봇v2 모듈을 넣는다 (음악·마크 연동 코드가 거기 있다). 웹에선 None


def _메인():
    if 메인 is None:
        raise RuntimeError("음악·마크 연동은 봇에서만 쓸 수 있어요")
    return 메인


def _음악():
    return _메인()


def _봇():
    """on_ready 에서 tools.봇 을 안 넣었어도 음악설정() 에서 받은 bot 을 쓴다"""
    if 봇 is not None:
        return 봇
    try:
        return _메인().음악봇
    except Exception:
        return None


def _길드와멤버(user_id):
    b = _봇()
    if b is None or not b.guilds:
        return None, None
    for g in b.guilds:
        m = g.get_member(int(user_id))
        if m:
            return g, m
    # 멤버 캐시에 없으면 봇이 통화방에 있는 서버, 아니면 첫 서버를 쓴다
    for g in b.guilds:
        if g.voice_client is not None:
            return g, None
    return b.guilds[0], None


async def _음악실행(함수):
    try:
        return await 함수()
    except Exception as e:
        return f"음악 기능 실행 중 오류가 발생했습니다: {e}"


음악방송_선언 = _선언(
    "music_broadcast",
    "봇의 음악방송(음성채널 접속)을 시작하거나 끝낸다. '음악방송 켜줘', '통화방 들어와', '음악방송 꺼' 같은 요청에 사용한다. "
    "채널을 말하지 않으면 유저가 지금 들어가 있는 음성채널로 들어간다.",
    {
        "action": {"type": "string", "enum": ["start", "stop"], "description": "start=시작/이동, stop=종료"},
        "channel": {"type": "string", "description": "음성채널: 1/2/3, 채널 이름 또는 ID (생략 가능)"},
    },
    ["action"],
)

노래신청_선언 = _선언(
    "request_song",
    "유튜브에서 노래를 찾아 음악방송 대기열에 넣고 재생한다. '○○ 틀어줘', '○○ 신청해줘' 같은 요청에 사용한다. "
    "채팅에 mp3 파일을 올려달라는 요청은 download_song 을 쓴다. 봇이 음성채널에 없으면 유저가 있는 음성채널로 자동으로 들어간다. "
    "'○○ 반복으로 틀어줘'처럼 그 곡만 계속 듣고 싶어 하면 repeat=true (스킵할 때까지 그 곡만 반복).",
    {"query": {"type": "string", "description": "노래 이름이나 검색어"},
     "repeat": {"type": "boolean", "description": "true 면 차례가 왔을 때 스킵할 때까지 이 곡만 반복"}},
    ["query"],
)

노래리스트_선언 = _선언(
    "music_queue",
    "음악방송 대기열(노래 리스트)을 보거나 조작한다. show=목록 보기, now=지금 곡, skip=다음 곡으로, clear=비우기, "
    "shuffle=섞기(count=섞을 횟수, 기본 1), move=순서 바꾸기(from_pos 곡을 to_pos 자리로), remove=from_pos 곡 빼기. "
    "번호는 목록에서 보이는 1부터 시작하는 번호이고, 재생 중이면 1번은 지금 나오는 곡이다. "
    "'5번 다음에 틀어줘'는 move from_pos=5 to_pos=2. 번호를 모르면 먼저 show 로 확인한다.",
    {
        "action": {"type": "string", "enum": ["show", "now", "skip", "clear", "shuffle", "move", "remove"]},
        "from_pos": {"type": "integer", "description": "옮기거나 뺄 곡 번호"},
        "to_pos": {"type": "integer", "description": "옮길 자리 번호"},
        "count": {"type": "integer", "description": "shuffle 할 횟수 (기본 1, 최대 1000)"},
    },
    ["action"],
)

플레이리스트_선언 = _선언(
    "playlist",
    "저장된 플레이리스트를 관리하고 재생한다. list_all=플레이리스트 이름 목록, show=곡 목록, create=만들기, delete=플레이리스트 통째로 삭제, "
    "add=노래 검색해서 추가(query), remove=곡 하나 삭제(from_pos), move=곡 순서 바꾸기(from_pos→to_pos), "
    "play=대기열에 넣고 재생, shuffle_play=섞어서 재생, repeat_play=다 돌면 처음부터 다시 도는 반복 재생, shuffle_repeat=섞어서 반복 재생, "
    "repeat_off=반복 끄기(name 없으면 전부, 곡은 대기열에 남음), stop=재생 중단 및 대기열 비우기, import_youtube=유튜브 플레이리스트 주소(query)로 곡 가져오기. "
    "delete 는 유저가 분명히 플레이리스트를 지우라고 했을 때만 쓴다.",
    {
        "action": {"type": "string", "enum": ["list_all", "show", "create", "delete", "add", "remove", "move",
                                              "play", "shuffle_play", "repeat_play", "shuffle_repeat", "repeat_off",
                                              "stop", "import_youtube"]},
        "name": {"type": "string", "description": "플레이리스트 이름"},
        "query": {"type": "string", "description": "add 의 노래 검색어, import_youtube 의 주소"},
        "from_pos": {"type": "integer", "description": "곡 번호"},
        "to_pos": {"type": "integer", "description": "옮길 자리 번호"},
    },
    ["action"],
)


async def _방송보장(M, g, m):
    """봇이 음성채널에 없으면 유저가 있는 채널로 들어간다. (앞에 붙일 문구, 성공여부)"""
    if g.voice_client is not None:
        return "", True
    문구 = await M.음악방송시작(g, None, m)
    if g.voice_client is None:
        return 문구 + "\n", False
    return 문구 + "\n", True


def 음악방송(user_id, action, channel=None):
    g, m = _길드와멤버(user_id)
    if g is None:
        return "봇이 서버 정보를 아직 못 불러왔습니다. 잠시 후 다시 시도해주세요."
    M = _음악()
    if action == "stop":
        return _음악실행(lambda: M.음악방송종료(g))
    return _음악실행(lambda: M.음악방송시작(g, channel, m))


def AI노래신청(user_id, query, repeat=False):
    g, m = _길드와멤버(user_id)
    if g is None:
        return "봇이 서버 정보를 아직 못 불러왔습니다. 잠시 후 다시 시도해주세요."
    M = _음악()

    async def 작업():
        앞, ok = await _방송보장(M, g, m)
        if not ok:
            return 앞 + "음성채널에 먼저 들어가 있거나 채널을 알려주세요."
        return 앞 + await M.음악신청(g, query, bool(repeat))
    return _음악실행(작업)


def 노래리스트(user_id, action, from_pos=None, to_pos=None, count=None):
    g, _ = _길드와멤버(user_id)
    if g is None:
        return "봇이 서버 정보를 아직 못 불러왔습니다. 잠시 후 다시 시도해주세요."
    M = _음악()
    if action == "show":
        곡들 = M.대기열목록()
        if not 곡들:
            return "대기열이 비어있습니다."
        재생 = M.현재곡(g) is not None
        줄 = [f"{i + 1}. {n}" + (" (재생 중)" if i == 0 and 재생 else "") for i, n in enumerate(곡들[:30])]
        if len(곡들) > 30:
            줄.append(f"... 외 {len(곡들) - 30}곡")
        return "\n".join(줄)
    if action == "now":
        곡 = M.현재곡(g)
        return f"지금 재생 중: {곡}" if 곡 else "지금 재생 중인 노래가 없습니다."
    if action == "skip":
        return M.음악스킵(g)
    if action == "clear":
        return M.대기열비우기()
    if action == "shuffle":
        return M.대기열셔플(g, count or 1)
    if action == "move":
        if from_pos is None or to_pos is None:
            return "from_pos 와 to_pos 가 필요합니다."
        return M.대기열순서변경(g, from_pos, to_pos)
    if action == "remove":
        return M.곡빼기(g, from_pos)
    return f"알 수 없는 동작: {action}"


def AI플레이리스트(user_id, action, name=None, query=None, from_pos=None, to_pos=None):
    g, m = _길드와멤버(user_id)
    M = _음악()
    if action == "list_all":
        목록 = M.플리목록()
        return "플레이리스트: " + (", ".join(목록) if 목록 else "없음")
    if action == "stop":
        return M.음악재생중단(g) if g else "봇이 서버 정보를 아직 못 불러왔습니다. 잠시 후 다시 시도해주세요."
    if action == "repeat_off":
        return M.플리반복끄기(name)
    if not name:
        return "플레이리스트 이름(name)이 필요합니다."
    if action == "show":
        곡들 = M.플리곡목록(name)
        if 곡들 is None:
            return f"{name} 플레이리스트가 없습니다."
        if not 곡들:
            return f"{name}에는 노래가 없습니다."
        줄 = [f"{i + 1}. {n}" for i, n in enumerate(곡들[:50])]
        if len(곡들) > 50:
            줄.append(f"... 외 {len(곡들) - 50}곡")
        return f"{name} ({len(곡들)}곡)\n" + "\n".join(줄)
    if action == "create":
        return M.플리생성(name)
    if action == "delete":
        관리 = {100000000000000005, 100000000000000008}
        if not (m and {r.id for r in m.roles} & 관리):
            return "플레이리스트 통째로 지우기는 관리자만 할 수 있습니다. 유저에게 그렇게 알려주세요."
        return M.플리제거(name)
    if action == "add":
        return _음악실행(lambda: M.플리추가(name, query))
    if action == "import_youtube":
        return _음악실행(lambda: M.플리유튜브(name, query))
    if action == "remove":
        return M.플리곡삭제(name, from_pos)
    if action == "move":
        if from_pos is None or to_pos is None:
            return "from_pos 와 to_pos 가 필요합니다."
        return M.플리순서(name, from_pos, to_pos)
    if action in ("play", "shuffle_play", "repeat_play", "shuffle_repeat"):
        if g is None:
            return "봇이 서버 정보를 아직 못 불러왔습니다. 잠시 후 다시 시도해주세요."

        async def 작업():
            앞, ok = await _방송보장(M, g, m)
            if not ok:
                return 앞 + "음성채널에 먼저 들어가 있거나 채널을 알려주세요."
            return 앞 + M.플리재생(g, name, 셔플여부=action.startswith("shuffle"), 반복="repeat" in action)
        return _음악실행(작업)
    return f"알 수 없는 동작: {action}"


# ============================================================
# TRPG (Cyberpunk 2020) — AI가 GM 노릇을 할 때 쓰는 툴
# ============================================================

현재채널 = None     # on_message 에서 tools.현재채널 = str(message.channel.id) 로 넣어준다


def _trpg():
    import TRPG
    return TRPG


def _채널(channel_id=None):
    return str(channel_id or 현재채널 or "0")


TRPG정보_선언 = _선언(
    "trpg_info",
    "Cyberpunk 2020 TRPG의 참고 자료를 본다. what=rules 는 판정·전투 규칙 요약, roles 는 역할 목록, "
    "weapons 는 무기 표, armor 는 방어구 표, skills 는 유저가 가진 스킬, "
    "skill_list 는 스킬 42종이 각각 무엇이고 어떤 능력치로 굴리는지, "
    "guide 는 특정 시스템의 자세한 설명(topic 에 주제를 넣는다). "
    "유저가 '이게 뭐야', '어떻게 하는 거야' 라고 물으면 guide 로 읽고 쉽게 풀어서 설명해라. "
    "GM으로서 판정 난이도를 정하거나 무기 이름을 고르기 전에도 확인한다.",
    {"what": {"type": "string", "enum": ["rules", "roles", "weapons", "armor", "skills", "skill_list", "guide"]},
     "topic": {"type": "string",
               "description": "what=guide 일 때의 주제: 시작/판정/능력치/스킬/역할/전투/부상/장비/사이버웨어/넷러닝/격투/차량/과거/연동"}},
    ["what"],
)

TRPG시트_선언 = _선언(
    "trpg_sheet",
    "대화 중인 유저의 TRPG 캐릭터 시트를 본다. 능력치, 스킬, 무기, 방어구, 누적 피해와 부상 상태를 알려준다. "
    "판정이나 전투를 시키기 전에 먼저 확인한다.",
)

TRPG생성_선언 = _선언(
    "trpg_create",
    "유저의 TRPG 캐릭터를 만든다. 역할(role)은 솔로/넷러너/록커보이/솔로테크/메드테크/미디어/캅/코퍼/픽서/노매드 중 하나. "
    "능력치와 역할 스킬은 자동으로 배분된다. 유저가 직접 캐릭터를 만들겠다고 할 때만 쓴다.",
    {"name": {"type": "string", "description": "캐릭터 이름"},
     "role": {"type": "string", "description": "역할"}},
    ["name", "role"],
)

TRPG판정_선언 = _선언(
    "trpg_roll",
    "유저에게 스킬 판정을 시킨다. 1D10 + 능력치 + 스킬 vs 난이도로 굴린다. "
    "stat 은 INT/REF/TECH/COOL/ATTR/LUCK/MA/BODY/EMP 중 하나. skill 은 시트에 있는 스킬 이름. "
    "difficulty 는 쉬움(10)/보통(15)/어려움(20)/매우어려움(25)/거의불가능(30) 또는 숫자. "
    "상황이 불리하면 modifier 에 음수를 준다. 10이 나오면 한 번 더 굴려 더하고(대성공), 1이 나오면 빼는(펌블) 처리는 자동이다. "
    "유저가 뭔가를 시도할 때마다 서술만 하지 말고 반드시 이 툴로 굴려서 결과를 정해라.",
    {"stat": {"type": "string", "description": "능력치 약어"},
     "skill": {"type": "string", "description": "스킬 이름 (없으면 생략)"},
     "difficulty": {"type": "string", "description": "난이도 이름 또는 숫자"},
     "modifier": {"type": "integer", "description": "상황 보정. 불리하면 음수"}},
    ["stat"],
)

TRPG공격_선언 = _선언(
    "trpg_attack",
    "유저가 NPC를 공격한다. 명중 판정, 부위 판정, 피해 계산, 방어구 관통, 스턴·데스 세이브까지 한 번에 처리한다. "
    "target 은 trpg_npc 로 먼저 등록한 NPC 이름이어야 한다. "
    "range 는 밀착/근거리/중거리/원거리/극원거리. burst 는 연사 발수(기관단총·소총만 의미 있음). "
    "weapon 을 비우면 유저가 든 무기를 쓴다.",
    {"target": {"type": "string", "description": "공격할 NPC 이름"},
     "weapon": {"type": "string", "description": "무기 이름 (생략하면 장착 무기)"},
     "range": {"type": "string", "description": "사거리 구간"},
     "modifier": {"type": "integer", "description": "상황 보정"},
     "burst": {"type": "integer", "description": "연사 발수"}},
    ["target"],
)

TRPG피해_선언 = _선언(
    "trpg_damage",
    "NPC가 유저를 때리거나, 함정·낙하·폭발 등으로 피해를 줄 때 쓴다. target 이 'me' 또는 비어 있으면 유저 본인, "
    "아니면 그 이름의 NPC에게 적용한다. amount 는 방어구를 이미 계산한 최종 피해면 그대로, "
    "아직 안 했으면 trpg_attack 쪽을 쓰는 게 정확하다. 스턴·데스 세이브는 자동으로 굴린다.",
    {"target": {"type": "string", "description": "'me' 또는 NPC 이름"},
     "amount": {"type": "integer", "description": "피해량"}},
    ["amount"],
)

TRPG_NPC_선언 = _선언(
    "trpg_npc",
    "전투에 쓸 NPC(적)를 만들고 관리한다. action=add 로 추가, list 로 현재 상태 확인, clear 로 전부 삭제. "
    "add 할 때 body=맷집(기본 5), ref=반사(기본 5), skill=무기 숙련(기본 4), armor=방어구 SP(기본 0), weapon=무기 이름. "
    "잡졸은 body 4~5/skill 3, 솔로급은 body 8/skill 8/armor 16 정도가 적당하다. "
    "전투를 시작하기 전에 반드시 적을 먼저 등록해라.",
    {"action": {"type": "string", "enum": ["add", "list", "clear"]},
     "name": {"type": "string", "description": "NPC 이름"},
     "body": {"type": "integer"}, "ref": {"type": "integer"},
     "skill": {"type": "integer"}, "armor": {"type": "integer"},
     "weapon": {"type": "string"}},
    ["action"],
)

TRPG이니시_선언 = _선언(
    "trpg_initiative",
    "전투 턴 순서를 관리한다. action=roll 은 유저의 이니셔티브(1D10+REF)를 굴리고, "
    "npc_name 을 주면 그 NPC 것을 굴린다. list 는 현재 순서, clear 는 초기화. "
    "전투가 시작되면 참가자 전원의 이니셔티브를 굴려라.",
    {"action": {"type": "string", "enum": ["roll", "list", "clear"]},
     "npc_name": {"type": "string", "description": "NPC 이니셔티브를 굴릴 때 그 이름"}},
    ["action"],
)

TRPG치료_선언 = _선언(
    "trpg_heal",
    "부상을 치료한다. action=heal 은 누적 피해를 amount 만큼 줄이고(생략하면 완치), "
    "stabilize 는 유저의 TECH+의료스킬로 안정화 판정을 해서 치명상 환자가 죽지 않게 만든다. "
    "target 은 'me' 또는 NPC 이름.",
    {"action": {"type": "string", "enum": ["heal", "stabilize"]},
     "target": {"type": "string", "description": "'me' 또는 NPC 이름"},
     "amount": {"type": "integer", "description": "회복량. 생략하면 완치"}},
    ["action"],
)


def TRPG정보(user_id, what, topic=None):
    T = _trpg()
    if what == "guide":
        return T.설명보기(topic)
    if what == "skill_list":
        return T.스킬설명(topic)
    if what == "rules":
        return (
            "[판정] 1D10 + 능력치 + 스킬 vs 난이도. "
            "난이도 쉬움 10 / 보통 15 / 어려움 20 / 매우어려움 25 / 거의불가능 30.\n"
            "주사위 10 = 한 번 더 굴려 더함(대성공), 1 = 한 번 더 굴려 뺌(펌블).\n"
            "[전투] 이니셔티브 1D10+REF. 명중은 1D10+REF+무기스킬 vs 사거리 목표치"
            "(밀착 10 / 근거리 15 / 중거리 20 / 원거리 25 / 극원거리 30).\n"
            "피해는 부위 1D10으로 정하고(1 머리, 2-4 몸통, 5-6 팔, 7-10 다리), "
            "머리는 피해 2배. 피해 − 방어구 SP − 체격보정 = 실제 피해.\n"
            "[부상] 누적 1-4 경상 / 5-8 중상(REF-2) / 9-12 위독(REF-4) / 13 이상 치명(REF-6).\n"
            "피해를 받을 때마다 스턴 세이브(1D10 ≤ BODY−부상페널티), 실패하면 기절. "
            "치명 상태면 데스 세이브도 굴리고 실패하면 사망. 안정화에 성공하면 더 안 죽는다."
        )
    if what == "roles":
        return "\n".join(f"{n} — 특수능력 {s[1]}: {s[2]} / 주요 스킬 {', '.join(s[3][:5])}"
                         for n, s in T.역할표.items())
    if what == "weapons":
        return "\n".join(f"{n} — {d[3]} 피해, 연사 {d[4]}, 사거리 {d[5]}m, {d[6]:,}eb ({d[7]})"
                         for n, d in T.무기.items())
    if what == "armor":
        return "\n".join(f"{n} — SP {d[0]}, 보호 부위 {','.join(d[1])}, {d[2]:,}eb" for n, d in T.방어구.items())
    if what == "skills":
        c = T.캐릭터(user_id)
        if c is None:
            return "캐릭터가 없습니다."
        return f"{c['name']} 의 스킬: " + (", ".join(f"{n} {lv}" for n, lv in c["스킬"].items()) or "없음")
    return "what 은 rules / roles / weapons / armor / skills 중 하나여야 합니다."


def TRPG시트(user_id):
    T = _trpg()
    c = T.캐릭터(user_id)
    if c is None:
        return "이 유저는 아직 TRPG 캐릭터가 없습니다. trpg_create 로 만들거나, 유저에게 만들라고 안내하세요."
    상태, 스턴, 치명 = T.부상정보(c["damage"])
    체격, btm = T.체격정보(c["stat_body"])
    능력 = " ".join(f"{k} {c[f'stat_{k.lower()}']}" for k in T.능력치순서)
    스킬 = ", ".join(f"{n} {lv}" for n, lv in c["스킬"].items()) or "없음"
    줄 = [f"{c['name']} ({c['role']}, 특수능력 Lv.{c['special']})",
          f"능력치: {능력}",
          f"스킬: {스킬}",
          f"무기: {c['weapon'] or '맨손'} / 방어구: {c['armor'] or '없음'} / 돈 {c['money']:,}eb",
          f"누적 피해 {c['damage']} — {상태} (체격 {체격}, 피해감소 {abs(btm)})",
          f"스턴 세이브 목표 {max(0, c['stat_body'] + 스턴)} 이하, 인간성 {T.인간성(c)}/{T.최대인간성(c)}"]
    if 치명 is not None:
        줄.append(f"치명상! 데스 세이브 {c['stat_body'] - 치명} 이하를 굴려야 삽니다.")
    return "\n".join(줄)


def TRPG생성(user_id, name, role):
    T = _trpg()
    ok, r = T.생성(user_id, name, role)
    if not ok:
        return r
    return "캐릭터를 만들었습니다.\n" + TRPG시트(user_id)


def TRPG판정(user_id, stat, skill=None, difficulty=None, modifier=0):
    T = _trpg()
    r, err = T.판정(user_id, stat, skill, difficulty, modifier)
    if err:
        return err
    줄 = (f"{r['이름']}: {r['식']} = {r['총합']} vs {r['난이도']}({r['목표']}) → "
          + ("성공" if r["성공"] else "실패"))
    if r["대성공"]:
        줄 += " [대성공! 주사위 10이 터졌다]"
    if r["펌블"]:
        줄 += " [펌블! 뭔가 크게 잘못됐다 — GM이 불운한 결과를 서술할 것]"
    return 줄


def TRPG공격(user_id, target, weapon=None, range=None, modifier=0, burst=1, channel_id=None):
    T = _trpg()
    r, err = T.공격(_채널(channel_id), 공격자_uid=user_id, 대상=target,
                   무기이름=weapon, 사거리=range or "중거리", 보정=modifier, 연사=burst)
    if err:
        return err
    return T.공격문장(r).replace("**", "")


def TRPG피해(user_id, amount, target=None, channel_id=None):
    T = _trpg()
    ch = _채널(channel_id)
    대상 = str(target or "me")
    isnpc = 대상.lower() != "me" and T.NPC(ch, 대상) is not None
    r, err = T.피해적용(ch, 대상 if isnpc else user_id, amount, NPC여부=isnpc)
    if err:
        return err
    줄 = [f"{r['이름']} 이(가) 피해 {amount} → 누적 {r['누적피해']} ({r['상태']})"]
    s = r.get("스턴세이브")
    if s:
        줄.append(f"스턴 세이브 {s['주사위']} vs {s['목표']} → " + ("버팀" if s["성공"] else "기절, 다음 턴 행동 불가"))
    d = r.get("데스세이브")
    if d:
        줄.append(f"데스 세이브 {d['주사위']} vs {d['목표']} → " + ("생존" if d["성공"] else "사망"))
    return "\n".join(줄)


def TRPG_NPC(user_id, action, name=None, body=None, ref=None, skill=None, armor=None, weapon=None, channel_id=None):
    T = _trpg()
    ch = _채널(channel_id)
    if action == "add":
        if not name:
            return "NPC 이름(name)이 필요합니다."
        return T.NPC추가(ch, name, body if body is not None else 5, ref if ref is not None else 5,
                        skill if skill is not None else 4, armor if armor is not None else 0,
                        weapon or "중권총")
    if action == "clear":
        return T.NPC정리(ch)
    목록 = T.NPC목록(ch)
    if not 목록:
        return "등록된 NPC가 없습니다."
    줄 = []
    for n, b, rf, sk, ar, dmg in 목록:
        상태, _, _ = T.부상정보(dmg)
        줄.append(f"{n}: BODY {b} REF {rf} 스킬 {sk} SP {ar} / 누적피해 {dmg} ({상태})")
    return "\n".join(줄)


def TRPG이니시(user_id, action, npc_name=None, channel_id=None):
    T = _trpg()
    ch = _채널(channel_id)
    if action == "roll":
        if npc_name:
            n = T.NPC(ch, npc_name)
            if n is None:
                return f"'{npc_name}' NPC가 없습니다."
            r, err = T.이니시굴림(ch, 이름=n["name"], REF=n["ref"])
        else:
            r, err = T.이니시굴림(ch, user_id=user_id)
        if err:
            return err
        return f"{r['이름']} 이니셔티브 1D10({r['주사위']}) + REF {r['REF']} = {r['값']}"
    if action == "clear":
        return T.이니시초기화(ch)
    목록 = T.이니시목록(ch)
    if not 목록:
        return "이니셔티브가 비어 있습니다."
    return "턴 순서: " + " > ".join(f"{n}({v})" for n, v in 목록)


def TRPG치료(user_id, action, target=None, amount=None, channel_id=None):
    T = _trpg()
    ch = _채널(channel_id)
    대상 = str(target or "me")
    if action == "heal":
        if 대상.lower() != "me" and T.NPC(ch, 대상) is not None:
            n = T.NPC(ch, 대상)
            새 = max(0, n["damage"] - int(amount)) if amount is not None else 0
            T.cursor.execute("UPDATE trpg_npcs SET damage = ? WHERE channel_id = ? AND name = ?", (새, ch, n["name"]))
            T.conn.commit()
            상태, _, _ = T.부상정보(새)
            return f"{n['name']} 누적 피해 {n['damage']} → {새} ({상태})"
        ok, msg = T.회복(user_id, amount)
        return msg
    if action == "stabilize":
        isnpc = 대상.lower() != "me" and T.NPC(ch, 대상) is not None
        r, err = T.안정화(user_id, 대상 if isnpc else user_id, NPC여부=isnpc, channel_id=ch)
        if err:
            return err
        return (f"{r['의사']} → {r['환자']} 안정화: 1D10({r['주사위']}) + TECH {r['TECH']} + 의료 {r['스킬레벨']} "
                f"= {r['총합']} vs {r['목표']} → " + ("성공, 더 이상 죽지 않는다" if r["성공"] else "실패, 계속 출혈"))
    return "action 은 heal 또는 stabilize 여야 합니다."


TRPG과거_선언 = _선언(
    "trpg_lifepath",
    "유저 캐릭터의 과거(라이프패스)를 본다. 출신, 집안, 성격, 가치관, 해마다 겪은 일이 들어 있다. "
    "action=show 는 보기, roll 은 새로 굴리기. 장면을 짤 때 이 과거에서 NPC나 사건을 끌어다 쓰면 좋다.",
    {"action": {"type": "string", "enum": ["show", "roll"]}},
)

TRPG사이버_선언 = _선언(
    "trpg_cyber",
    "사이버웨어를 관리한다. action=list 는 설치 가능한 목록과 지금 달고 있는 것, install 은 설치(돈과 인간성이 깎인다), "
    "remove 는 제거, sanity 는 인간성이 낮을 때 COOL 로 자제력 판정. "
    "인간성이 10 깎일 때마다 EMP 가 1 떨어지고, 0 이 되면 사이버사이코가 되어 캐릭터를 잃는다. "
    "유저가 분명히 원할 때만 install 하고, 인간성이 20 아래면 경고해라.",
    {"action": {"type": "string", "enum": ["list", "install", "remove", "sanity"]},
     "name": {"type": "string", "description": "사이버웨어 이름"}},
    ["action"],
)

TRPG넷_선언 = _선언(
    "trpg_net",
    "넷러닝을 돌린다. action=create 는 침입할 시스템을 만들고(name, difficulty: 쉬움/보통/어려움/매우어려움), "
    "run 은 노드 하나를 뚫고(system, program), list 는 현재 상태, clear 는 삭제. "
    "ICE 와의 싸움은 프로그램 강도 대결이라 프로그램 없이 덤비면 거의 진다. "
    "블랙 ICE(헬하운드, 리퍼)에 지면 넷러너가 실제로 뇌에 피해를 입고 죽을 수 있다. "
    "넷러닝 장면을 시작하면 먼저 create 로 시스템을 만들어라.",
    {"action": {"type": "string", "enum": ["create", "run", "list", "clear"]},
     "name": {"type": "string", "description": "만들 시스템 이름"},
     "difficulty": {"type": "string", "description": "시스템 난이도"},
     "system": {"type": "string", "description": "침입할 시스템 이름"},
     "program": {"type": "string", "description": "쓸 프로그램 이름"},
     "modifier": {"type": "integer"}},
    ["action"],
)

TRPG운전_선언 = _선언(
    "trpg_drive",
    "운전 판정을 한다. situation 은 평범한 주행/빗길·야간/추격전/곡예 주행/불가능한 묘기. "
    "크게 실패하면 충돌 피해가 자동으로 굴려진다. 추격전이나 도주 장면에서 쓴다.",
    {"situation": {"type": "string", "description": "운전 상황"},
     "vehicle": {"type": "string", "description": "타고 있는 차량 이름"},
     "modifier": {"type": "integer"}},
)

TRPG격투_선언 = _선언(
    "trpg_melee",
    "맨손·근접 격투를 한다. technique 는 타격/발차기/막기/회피/던지기/잡기/탈출/조르기/넘어뜨리기/그래플. "
    "배운 마셜아츠 유파에 따라 기술마다 보너스가 다르다. target 에 NPC 이름을 주면 피해까지 적용된다. "
    "action=learn 과 name 으로 유파를 배울 수도 있다(가라테/유도/복싱/무에타이/최리풋/합기도/동물권/태권도/사바테/레슬링/카포에라).",
    {"technique": {"type": "string", "description": "격투 기술"},
     "target": {"type": "string", "description": "대상 NPC 이름"},
     "action": {"type": "string", "enum": ["strike", "learn", "list"]},
     "name": {"type": "string", "description": "배울 유파 이름"},
     "modifier": {"type": "integer"}},
)

TRPG연동_선언 = _선언(
    "trpg_link",
    "TRPG 캐릭터를 디스코드 RPG 캐릭터와 묶는다. action=create 는 RPG 캐릭터의 이름을 가져와 "
    "TRPG 캐릭터를 새로 만들고(role 필요), exchange 는 골드와 유로달러를 환전한다"
    "(dir: '골드→eb' 또는 'eb→골드', qty). "
    "create 는 RPG 캐릭터의 이름만 가져올 뿐이고, 능력치는 CP2020 규칙대로 새로 굴린다. "
    "RPG 의 힘·체력·속도·지능은 TRPG 에서 전혀 쓰이지 않는다.",
    {"action": {"type": "string", "enum": ["create", "exchange"]},
     "role": {"type": "string", "description": "create 할 때의 역할"},
     "dir": {"type": "string", "description": "환전 방향"},
     "qty": {"type": "integer", "description": "환전 수량"}},
    ["action"],
)


def TRPG과거(user_id, action="show"):
    T = _trpg()
    if action == "roll":
        if T.캐릭터(user_id) is None:
            return "캐릭터가 없습니다."
        d = T.라이프패스생성()
        T.라이프패스저장(user_id, d)
    else:
        d = T.라이프패스읽기(user_id)
    return T.라이프패스글(d).replace("**", "")


def TRPG사이버(user_id, action, name=None):
    T = _trpg()
    if action == "list":
        c = T.캐릭터(user_id)
        줄 = ["[설치 가능]"]
        줄 += [f"{n} ({d[0]}) — {d[2]:,}eb · 인간성 {d[1]} · {d[3]}" for n, d in T.사이버웨어.items()]
        if c:
            가진 = T.설치목록(user_id)
            줄.append("[지금 달고 있는 것] " + (", ".join(f"{n}×{q}" for n, q in 가진.items()) if 가진 else "없음"))
            줄.append(f"인간성 {T.인간성(c)} / {T.최대인간성(c)}")
        return "\n".join(줄)
    if action == "install":
        ok, msg = T.사이버설치(user_id, name)
        return msg.replace("**", "")
    if action == "remove":
        ok, msg = T.사이버제거(user_id, name)
        return msg
    if action == "sanity":
        r, err = T.사이버사이코판정(user_id)
        if err:
            return err
        return (f"{r['이름']} 자제력 판정 (남은 인간성 {r['남은인간성']}): "
                f"1D10({r['기록']}) + COOL {r['COOL']} = {r['총합']} vs {r['목표']} → "
                + ("성공, 아직 사람이다" if r["성공"] else "실패! 이성을 잃고 가까운 것부터 부순다"))
    return "action 이 올바르지 않습니다."


def TRPG넷(user_id, action, name=None, difficulty=None, system=None, program=None,
          modifier=0, channel_id=None):
    T = _trpg()
    ch = _채널(channel_id)
    if action == "create":
        d = T.넷시스템생성(ch, name or "무명 시스템", difficulty or "보통")
        줄 = [f"{d['이름']} ({d['난이도']}, 노드 {len(d['노드'])}개) 구축"]
        for nd in d["노드"]:
            줄.append(f"  노드 {nd['번호']} [{nd['종류']}] — {', '.join(nd['ICE']) or '경비 없음'}")
        return "\n".join(줄)
    if action == "run":
        r, err = T.넷침입(user_id, ch, system, program, modifier)
        if err:
            return err
        return T.넷침입글(r).replace("**", "")
    if action == "clear":
        return T.넷시스템삭제(ch, name)
    목록 = T.넷시스템목록(ch)
    if not 목록:
        return "이 방에 시스템이 없습니다. create 로 먼저 만드세요."
    줄 = []
    for 이름 in 목록:
        d = T.넷시스템(ch, 이름)
        뚫린 = sum(1 for n in d["노드"] if n["뚫림"])
        줄.append(f"{이름} ({d['난이도']}) — {뚫린}/{len(d['노드'])} 돌파, 경보 {d['경보']}회")
        for n in d["노드"]:
            줄.append(f"  {'뚫림' if n['뚫림'] else '잠김'} 노드 {n['번호']} [{n['종류']}] {', '.join(n['ICE']) or '-'}")
    return "\n".join(줄)


def TRPG운전(user_id, situation=None, vehicle=None, modifier=0):
    T = _trpg()
    r, err = T.운전판정(user_id, situation or "추격전", modifier, vehicle)
    if err:
        return err
    줄 = (f"{r['이름']} 운전 ({r['상황']}" + (f", {r['차량']}" if r["차량"] else "") + "): "
          f"1D10({r['기록']}) + REF {r['REF']} + 운전 {r['스킬레벨']} = {r['총합']} vs {r['목표']} → "
          + ("성공" if r["성공"] else "실패"))
    if r["충돌"]:
        줄 += f"\n충돌! 피해 {r['충돌']['피해식']} = {r['충돌']['피해']} (아직 적용 안 됨, trpg_damage 로 적용)"
    return 줄


def TRPG격투(user_id, technique=None, target=None, action=None, name=None, modifier=0, channel_id=None):
    T = _trpg()
    if action == "list":
        return "\n".join(f"{n} (난이도 {d[0]}) — {d[2]} / "
                         + ", ".join(f"{k} +{v}" for k, v in d[1].items())
                         for n, d in T.마셜아츠.items())
    if action == "learn":
        ok, msg = T.유파배우기(user_id, name)
        return msg.replace("**", "")
    r, err = T.격투판정(user_id, technique or "타격", 대상=target,
                      channel_id=_채널(channel_id), 보정=modifier)
    if err:
        return err
    return T.격투글(r).replace("**", "")


def TRPG연동(user_id, action, role=None, dir=None, qty=None):
    T = _trpg()
    if action == "create":
        if not role:
            return "역할(role)이 필요합니다. 솔로/넷러너/록커보이/솔로테크/메드테크/미디어/캅/코퍼/픽서/노매드 중 하나."
        ok, msg = T.RPG연동생성(user_id, role)
        if not ok:
            return msg
        return msg.replace("**", "") + "\n\n" + TRPG시트(user_id)
    if action == "exchange":
        ok, msg = T.골드환전(user_id, dir or "골드→eb", qty or 0)
        return msg.replace("**", "")
    return "action 은 create 또는 exchange 입니다."


# ============================================================
# 웹 검색 / 웹페이지 읽기 — 구글 툴 대신 직접 긁어온다 (한국어는 네이버, 그 밖은 Bing)
# DB 를 안 쓰므로 봇에서는 스레드로 돌린다 (오래 걸려도 봇이 안 멈춘다)
# ============================================================

import base64
import ipaddress
import socket
from urllib.parse import urlparse, parse_qs, urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

_브라우저 = {
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
검색_결과수 = 6
페이지_최대글자 = 4000
페이지_최대바이트 = 2 * 1024 * 1024

웹검색_선언 = _선언(
    "web_search",
    "인터넷에서 검색한다. 최신 소식, 사실 확인, 모르는 것, 날짜·가격·일정처럼 바뀌는 정보가 필요할 때 쓴다. "
    "제목·주소·요약이 돌아오고, 자세한 내용이 필요하면 그 주소로 read_webpage 를 이어서 호출한다. "
    "답할 때는 어디서 찾았는지(사이트 이름) 짧게 밝힌다.",
    {"query": {"type": "string", "description": "검색어. 짧고 핵심만. 한국 정보면 한국어로"},
     "news": {"type": "boolean", "description": "뉴스만 찾을 때 true"}},
    ["query"],
)
웹페이지_선언 = _선언(
    "read_webpage",
    "웹페이지 주소를 열어 본문 글을 읽는다. web_search 로 찾은 주소나 유저가 준 주소에 쓴다. 긴 글은 앞부분만 온다.",
    {"url": {"type": "string", "description": "http 또는 https 주소"}},
    ["url"],
)


def _bing실제주소(u):
    """bing.com/ck/a?...&u=a1<base64> 같은 중간 주소를 원래 주소로 푼다"""
    if "bing.com/ck/a" in u:
        v = parse_qs(urlparse(u).query).get("u", [""])[0]
        if v.startswith("a1"):
            b = v[2:] + "=" * (-len(v[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode()
            except Exception:
                pass
    return u


def _네이버검색(q, news):
    """네이버 결과. 클래스 이름이 무작위라서 링크 주소별로 묶어 제목·요약을 고른다"""
    r = httpx.get("https://search.naver.com/search.naver",
                  params={"query": q, "where": "news" if news else "web"}, headers=_브라우저, timeout=10)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "html.parser")
    묶음 = {}
    for a in s.find_all("a", href=True):
        h = a["href"]
        if not h.startswith("http"):
            continue
        p = urlparse(h)
        if p.hostname is None or p.hostname.endswith(("naver.com", "naver.net")) or p.path in ("", "/"):
            continue
        글 = a.get_text(" ", strip=True).replace("새 창 열림", "").strip()
        if len(글) < 6 or "›" in 글:
            continue
        묶음.setdefault(h, []).append(글)
    결과 = []
    for h, 글들 in 묶음.items():
        제목 = 글들[0]
        요약 = max(글들[1:], key=len) if len(글들) > 1 else ""
        결과.append((제목, h, f"[{urlparse(h).hostname}] {요약}"))
        if len(결과) >= 검색_결과수:
            break
    return 결과


def _bing검색(q, news):
    주소 = "https://www.bing.com/news/search" if news else "https://www.bing.com/search"
    r = httpx.get(주소, params={"q": q, "setlang": "ko", "cc": "KR"}, headers=_브라우저, timeout=10, follow_redirects=True)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "html.parser")
    결과 = []
    for 칸 in (s.select("div.news-card, div.newsitem") if news else s.select("li.b_algo")):
        a = (칸.select_one("a.title") or 칸.select_one("a")) if news else 칸.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        요약 = 칸.select_one(".snippet") if news else (칸.select_one(".b_caption p") or 칸.select_one("p"))
        주소 = _bing실제주소(a["href"])
        결과.append((a.get_text(" ", strip=True), 주소,
                     f"[{urlparse(주소).hostname}] " + (요약.get_text(" ", strip=True) if 요약 else "")))
        if len(결과) >= 검색_결과수:
            break
    return 결과


def 웹검색(query, news=False):
    if BeautifulSoup is None:
        return "검색 기능에 필요한 beautifulsoup4 가 설치되어 있지 않습니다."
    q = str(query or "").strip()
    if not q:
        return "검색어가 비어 있습니다."
    한글 = bool(re.search(r"[가-힣]", q))
    순서 = [_네이버검색, _bing검색] if 한글 else [_bing검색, _네이버검색]
    결과, 오류 = [], None
    for 검색 in 순서:
        try:
            결과 = 검색(q, bool(news))
        except Exception as e:
            오류 = e
            continue
        if len(결과) >= 2:
            break
    if not 결과:
        return f"'{q}' 검색 결과를 찾지 못했습니다." + (f" ({오류})" if 오류 else "")
    return f"'{q}' {'뉴스 ' if news else ''}검색 결과 (오늘 {datetime.now():%Y-%m-%d}):\n" + "\n".join(
        f"{i}. {t[:120]}\n   {u}\n   {요약[:250]}" for i, (t, u, 요약) in enumerate(결과, 1))


def _공개주소인가(호스트):
    """내부망·자기 자신·예약 주소면 False (AI 가 공유기·로컬 서버를 열어보지 못하게)"""
    try:
        주소들 = {i[4][0] for i in socket.getaddrinfo(호스트, None)}
    except socket.gaierror:
        return False
    try:
        우리서버 = {i[4][0] for i in socket.getaddrinfo(os.environ.get("MC_HOST", MC_주소), None)}
    except socket.gaierror:
        우리서버 = set()
    for a in 주소들:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast or a in 우리서버:
            return False
    return bool(주소들)


def 웹페이지읽기(url):
    if BeautifulSoup is None:
        return "웹페이지 읽기에 필요한 beautifulsoup4 가 설치되어 있지 않습니다."
    주소 = str(url or "").strip()
    for _ in range(4):                                   # 리다이렉트는 직접 따라가며 매번 주소를 검사한다
        p = urlparse(주소)
        if p.scheme not in ("http", "https") or not p.hostname:
            return "http 또는 https 주소만 열 수 있습니다."
        if not _공개주소인가(p.hostname):
            return "내부망이나 로컬 주소는 열 수 없습니다."
        try:
            with httpx.stream("GET", 주소, headers=_브라우저, timeout=10, follow_redirects=False) as r:
                if r.is_redirect:
                    주소 = urljoin(주소, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400:
                    return f"페이지를 열지 못했습니다 (HTTP {r.status_code})."
                종류 = r.headers.get("content-type", "")
                if not any(x in 종류 for x in ("text/html", "text/plain", "application/xhtml")):
                    return f"글이 아닌 파일이라 읽지 않았습니다 ({종류.split(';')[0] or '알 수 없음'})."
                몸 = b""
                for 조각 in r.iter_bytes():
                    몸 += 조각
                    if len(몸) > 페이지_최대바이트:
                        break
                인코딩 = r.encoding or "utf-8"
        except Exception as e:
            return f"페이지를 열지 못했습니다: {e}"
        break
    else:
        return "리다이렉트가 너무 많습니다."
    try:
        글 = 몸.decode(인코딩, errors="replace")
    except LookupError:
        글 = 몸.decode("utf-8", errors="replace")
    if "text/plain" in 종류:
        제목, 본문 = "", 글
    else:
        s = BeautifulSoup(글, "html.parser")
        for t in s(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]):
            t.decompose()
        제목 = s.title.get_text(strip=True) if s.title else ""
        뼈대 = s.find("article") or s.find("main") or s.body or s
        본문 = 뼈대.get_text("\n", strip=True)
    본문 = re.sub(r"\n{2,}", "\n", re.sub(r"[ \t ]+", " ", 본문)).strip()
    if not 본문:
        return "본문 글을 찾지 못했습니다. (자바스크립트로만 그려지는 페이지일 수 있습니다)"
    잘림 = len(본문) > 페이지_최대글자
    return (f"[{제목[:100]}] {주소}\n" if 제목 else f"{주소}\n") + 본문[:페이지_최대글자] + ("\n…(뒷부분 생략)" if 잘림 else "")


# ============================================================
# AI 프롬프트 — 대화하는 유저 본인의 AI 성격·말투를 바꾼다 (!AI프롬포트 와 같은 칸)
# ============================================================

기본_프롬프트 = ""          # 봇 시작 때 tools.기본_프롬프트 = 기본_프롬프트 로 넣어준다
프롬프트_최대길이 = 3000

AI프롬프트_선언 = _선언(
    "ai_prompt",
    "대화 중인 유저 본인이 쓰는 AI(너)의 성격·말투·규칙 프롬프트를 보거나 바꾼다. 바뀐 내용은 다음 메시지부터 적용된다. "
    "유저가 '말투 바꿔줘', '앞으로 존댓말 써', '이런 성격으로 해줘', '프롬프트 보여줘', '원래대로 돌려줘' 처럼 "
    "직접 원할 때만 쓴다. 유저 본인이 자기 AI 를 바꾸는 것이라 허용된 일이다. "
    "유저가 '앞으로', '항상', '계속', '이제부터' 처럼 다음 대화에서도 지킬 말투·규칙을 말하면 그 자리에서 따르기만 하지 말고 "
    "반드시 이 툴로 저장한다. 저장하지 않으면 다음 대화에서 잊어버린다. "
    "action: show=지금 프롬프트 보기, set=통째로 바꾸기, append=끝에 새 규칙 덧붙이기, reset=기본 프롬프트로. "
    "말투(반말/존댓말)·이름·호칭·성격처럼 이미 적혀 있는 걸 바꾸는 요청이면 append 하지 말고 "
    "반드시 먼저 show 로 보고, 부딪히는 문장을 고치거나 지운 전체를 set 으로 넣는다 (안 그러면 옛 규칙과 섞인다). "
    "append 는 기존 내용과 전혀 겹치지 않는 새 규칙에만 쓴다. "
    "set·append 의 text 는 2인칭 지시문('너는 ~다', '~해라')으로 정리해서 넣고, 바꾸기 전에 어떻게 바꿀지 한 줄로 말해준다.",
    {"action": {"type": "string", "enum": ["show", "set", "append", "reset"]},
     "text": {"type": "string", "description": "set 이면 새 프롬프트 전체, append 면 덧붙일 규칙"}},
    ["action"],
)


def AI프롬프트(user_id, action, text=None):
    cursor.execute("SELECT ai_prompt FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        return "아직 등록되지 않은 유저입니다. !출석 을 먼저 사용해주세요."
    지금 = row[0] if row[0] and row[0] != "NULL" else 기본_프롬프트
    if action == "show":
        return f"지금 프롬프트 ({len(지금)}자):\n{지금}"
    if action == "reset":
        새것 = 기본_프롬프트
    elif action in ("set", "append"):
        글 = str(text or "").strip()
        if not 글:
            return "바꿀 내용(text)이 비어 있습니다."
        새것 = 글 if action == "set" else f"{지금.rstrip()} {글}"
    else:
        return "action 은 show / set / append / reset 중 하나여야 합니다."
    if not 새것:
        return "기본 프롬프트를 아직 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
    if len(새것) > 프롬프트_최대길이:
        return f"프롬프트가 너무 깁니다 ({len(새것)}자). {프롬프트_최대길이}자 이하로 줄여주세요."
    cursor.execute("UPDATE users SET ai_prompt = ? WHERE user_id = ?", (새것, user_id))
    conn.commit()
    return (f"프롬프트를 {'기본으로 되돌렸' if action == 'reset' else '바꿨'}습니다. 다음 메시지부터 적용됩니다. "
            f"({len(지금)}자 → {len(새것)}자)\n새 프롬프트:\n{새것}")


경제_툴 = {
    "get_my_balance", "get_stock_price", "buy_stock", "sell_stock", "get_my_portfolio",
    "get_my_loan", "take_loan", "repay_loan", "get_mining_status", "list_mining_rigs",
    "buy_mining_rig", "sell_mining_rig", "list_coin_conversions", "convert_satoshi",
    "pay_electricity", "sell_all_stocks", "buy_stock_max", "sell_all_mining_rigs",
    "leverage_open", "leverage_close", "get_my_leverage",
    "stock_order", "get_stock_events", "get_my_stock_tax", "get_etf_info",
}
RPG_툴 = {
    "rpg_status", "rpg_allocate_stat", "rpg_shop", "rpg_buy", "rpg_sell", "rpg_equip",
    "rpg_enhance", "rpg_gold_exchange", "rpg_pet", "rpg_achievements",
}


# ============================================================
# 10-1. JAS 모드 플레이어 API (마크 안 스탠드·직업·레벨·인벤토리)
#  - 우리봇v2.py 마크 연동 칸을 tools.메인 으로 쓴다. DB 는 호출마다 새 연결이라 스레드에서 돌려도 된다
# ============================================================

def _마크():
    return _메인()


def _관리자uid(user_id):
    봇 = _봇()
    for g in (봇.guilds if 봇 else []):
        m = g.get_member(int(user_id))
        if m and {r.id for r in m.roles} & {100000000000000005, 100000000000000008}:
            return True
    return False


def _마크닉찾기(user_id, player):
    """player 가 비면 본인 연결 닉, 마크 닉이면 그대로, 디코 멤버 이름이면 그 사람 연결 닉.
    반환 (닉, 오류문구)"""
    M = _마크()
    if not player:
        닉 = M.마크연결된닉(user_id)
        if not 닉:
            return None, "이 유저는 마크 닉이 연결되어 있지 않습니다. '!마크연동 [닉네임]' 으로 연결하거나 닉네임을 알려달라고 하세요."
        return 닉, None
    player = str(player).strip().lstrip("@")
    숫자 = re.sub(r"\D", "", player)
    if not (숫자 and len(숫자) >= 15) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        return player, None
    찾음 = _마크멤버찾기(player)
    if len(찾음) > 1:
        return None, f"'{player}' 에 해당하는 멤버가 여럿입니다: " + ", ".join(m.display_name for m in 찾음[:10])
    if 찾음:
        닉 = M.마크연결된닉(찾음[0].id)
        if 닉:
            return 닉, None
        return None, f"{찾음[0].display_name} 님은 마크 닉이 연결되어 있지 않습니다."
    return None, f"'{player}' 에 해당하는 마크 닉이나 디코 멤버를 찾지 못했습니다."


def _마크멤버찾기(이름):
    """스레드에서 돈다 — 공유 cursor 를 쓰는 _멤버찾기 대신 디코 캐시와 새 DB 연결로 찾는다"""
    봇 = _봇()
    길드 = 봇.guilds if 봇 else []
    숫자 = re.sub(r"\D", "", 이름)
    if 숫자 and len(숫자) >= 15:
        m = next((g.get_member(int(숫자)) for g in 길드 if g.get_member(int(숫자))), None)
        return [m] if m else []
    낮춤 = 이름.lower()
    정확, 부분 = {}, {}
    for g in 길드:
        for m in g.members:
            이름들 = {m.display_name.lower(), m.name.lower()}
            if 낮춤 in 이름들:
                정확[m.id] = m
            elif any(낮춤 in x for x in 이름들):
                부분[m.id] = m
    if 정확 or 부분:
        return list((정확 or 부분).values())
    c = sqlite3.connect(os.path.join(BASE_DIR, "bot.db"), timeout=15)
    try:
        r = c.execute("SELECT user_id FROM users WHERE name = ?", (이름,)).fetchone()
    finally:
        c.close()
    if r and str(r[0]).isdigit():
        m = next((g.get_member(int(r[0])) for g in 길드 if g.get_member(int(r[0]))), None)
        return [m] if m else []
    return []


def _마크오류(e):
    return _마크().마크오류문구(e)


def _마크요약줄(p, 주인=None):
    M = _마크()
    u = 주인.get((p.get("name") or "").lower()) if 주인 else None
    줄 = f"{p.get('name')}"
    if u:
        봇 = _봇()
        m = next((g.get_member(u) for g in (봇.guilds if 봇 else []) if g.get_member(u)), None)
        줄 += f"(디코: {m.display_name if m else u})"
    줄 += f" | {'접속 중' if p.get('online', True) else '오프라인'} | JAS Lv.{p.get('rpgLevel', 0)} | 마크 Lv.{p.get('mcLevel', 0)}"
    줄 += f" | 스탠드: {M._마크스탠드(p)} | 직업: {M._마크직업(p)}"
    if p.get("lastSeen") and not p.get("online", True):
        줄 += f" | 마지막 저장 {datetime.fromtimestamp(p['lastSeen'] / 1000):%Y-%m-%d %H:%M}"
    return 줄


마크플레이어_선언 = types.FunctionDeclaration(
    name="mc_player_info",
    description=(
        "마인크래프트 서버(JAS 모드)의 한 플레이어 정보를 조회한다: 스탠드, 직업, JAS RPG 레벨·경험치·마나·스탯, "
        "마크 경험치 레벨, 체력·배고픔, 있는 차원, 게임모드, 든 아이템·갑옷, 접속 여부. 오프라인도 마지막 저장 기준으로 된다. "
        "'내 스탠드 뭐야?', 'Steve 레벨 몇이야?', '철수 지금 어디 있어?' 같은 질문에 쓴다."
    ),
    parameters={"type": "object", "properties": {
        "player": {"type": "string", "description": "마크 닉네임, 또는 디코 멤버 이름/멘션. 비우면 대화 중인 유저 본인(연결된 닉)"},
    }},
)


def 마크플레이어(user_id, player=None):
    M = _마크()
    닉, 오류 = _마크닉찾기(user_id, player)
    if 오류:
        return 오류
    try:
        p = M.마크api().player(닉, inventory=True)
    except Exception as e:
        return _마크오류(e)
    if p is None:
        return f"'{닉}' 은 마크 서버에 들어온 적이 없습니다."
    r, m = p.get("rpg") or {}, p.get("minecraft") or {}
    줄 = [f"닉네임: {p.get('name')} ({'접속 중' if p.get('online') else '오프라인, 마지막 저장 기준'})"]
    주인 = M.마크닉주인(p.get("name") or 닉)
    if 주인:
        줄.append(f"연결된 디코 계정 ID: {주인}")
    줄.append(f"스탠드: {M._마크스탠드(p)}")
    줄.append(f"직업: {M._마크직업(p)}")
    다음 = r.get("expToNext")
    줄.append(f"JAS RPG: Lv.{r.get('level', 0)}/{r.get('maxLevel', 200)}, 경험치 {r.get('exp', 0)}"
              + (f"/{다음}" if 다음 else " (최대 레벨)")
              + f", 마나 {r.get('mana', 0):.0f}/{r.get('maxMana', 0):.0f}, 남은 스킬포인트 {r.get('skillPoints', 0)}")
    스탯 = r.get("stats") or {}
    줄.append("스탯(각 최대 45): " + ", ".join(f"{이름} {스탯.get(k, 0)}" for k, 이름 in M._마크스탯))
    마크 = f"마크: 경험치 Lv.{m.get('level', 0)} ({m.get('progress', 0) * 100:.0f}%)"
    if "health" in m:
        마크 += f", 체력 {m['health']:.0f}" + (f"/{m['maxHealth']:.0f}" if m.get("maxHealth") else "") + " (20=하트10칸)"
    if "food" in m:
        마크 += f", 배고픔 {m['food']}/20"
    if m.get("dimension"):
        마크 += f", 차원 {M._마크차원.get(m['dimension'], m['dimension'])}"
    if m.get("gameMode"):
        마크 += f", {M._마크모드.get(m['gameMode'], m['gameMode'])}"
    줄.append(마크)
    inv = p.get("inventory")
    if inv:
        든것 = next((it for it in inv["main"] if it["slot"] == inv.get("selectedSlot")), None)
        줄.append(f"손에 든 것: {든것['name']} x{든것['count']}" if 든것 else "손에 든 것: 없음")
        갑옷 = [inv["armor"][k]["name"] for k in ("head", "chest", "legs", "feet") if inv["armor"].get(k)]
        줄.append("갑옷: " + (", ".join(갑옷) or "없음"))
        if inv.get("offhand"):
            줄.append(f"왼손: {inv['offhand']['name']}")
    return "\n".join(줄)


마크접속자_선언 = types.FunctionDeclaration(
    name="mc_online_players",
    description=(
        "마인크래프트 서버에 지금 접속한 플레이어들의 스탠드·직업·JAS 레벨·마크 레벨과 연결된 디코 계정을 조회한다. "
        "'지금 접속한 사람들 스탠드 뭐야?', '누가 레벨 제일 높아?(접속자 중)' 같은 질문에 쓴다. "
        "서버가 켜져 있는지만 궁금하면 get_mc_server_status 를 쓴다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 마크접속자():
    M = _마크()
    try:
        목록 = M.마크api().players()
    except Exception as e:
        return _마크오류(e)
    if not 목록:
        return "지금 마크 서버에 접속한 사람이 없습니다."
    주인 = M.마크주인목록()
    return f"접속자 {len(목록)}명\n" + "\n".join(_마크요약줄(p, 주인) for p in 목록)


마크전체_선언 = types.FunctionDeclaration(
    name="mc_all_players",
    description=(
        "마인크래프트 서버에 한 번이라도 들어온 모든 플레이어(오프라인 포함) 목록. 스탠드·직업·JAS 레벨·마크 레벨·마지막 접속. "
        "'JAS 레벨 순위', '더 월드 가진 사람 누구야?', '닌자 직업 누구누구야?', '최근에 누가 들어왔어?' 같은 질문에 쓴다."
    ),
    parameters={"type": "object", "properties": {
        "sort": {"type": "string", "enum": ["recent", "rpg_level", "mc_level"],
                 "description": "정렬: recent=최근 접속 순(기본), rpg_level=JAS 레벨 높은 순, mc_level=마크 레벨 높은 순"},
        "stand": {"type": "string", "description": "이 글자가 들어간 스탠드를 가진 사람만 (예: '더 월드')"},
        "job": {"type": "string", "description": "이 직업을 가진 사람만 (예: '닌자')"},
        "limit": {"type": "integer", "description": "최대 몇 명 (기본 30)"},
    }},
)


def 마크전체(sort="recent", stand=None, job=None, limit=30):
    M = _마크()
    try:
        목록 = M.마크api().all_players()
    except Exception as e:
        return _마크오류(e)
    if stand:
        목록 = [p for p in 목록 if p.get("stand") and stand.replace(" ", "") in p["stand"]["name"].replace(" ", "")]
    if job:
        목록 = [p for p in 목록 if any(job in j["name"] or job == j["id"] for j in p.get("jobs") or [])]
    if sort == "rpg_level":
        목록.sort(key=lambda p: -(p.get("rpgLevel") or 0))
    elif sort == "mc_level":
        목록.sort(key=lambda p: -(p.get("mcLevel") or 0))
    if not 목록:
        return "조건에 맞는 플레이어가 없습니다."
    try:
        limit = max(1, min(int(limit or 30), 60))
    except (TypeError, ValueError):
        limit = 30
    주인 = M.마크주인목록()
    줄 = [f"{i + 1}. " + _마크요약줄(p, 주인) for i, p in enumerate(목록[:limit])]
    if len(목록) > limit:
        줄.append(f"... 외 {len(목록) - limit}명")
    return f"총 {len(목록)}명\n" + "\n".join(줄)


마크인벤_선언 = types.FunctionDeclaration(
    name="mc_player_inventory",
    description=(
        "마인크래프트 플레이어의 인벤토리(핫바·가방·갑옷·왼손, 선택하면 엔더상자까지)를 보거나 특정 아이템을 찾아 개수를 센다. "
        "본인 것만 볼 수 있고 다른 사람 것은 관리자만 볼 수 있다. "
        "'내 인벤에 다이아 몇 개야?', '성인의 유해 갖고 있어?', '내 엔더상자 뭐 있어?' 같은 질문에 쓴다."
    ),
    parameters={"type": "object", "properties": {
        "player": {"type": "string", "description": "마크 닉네임 또는 디코 멤버 이름. 비우면 본인"},
        "item": {"type": "string", "description": "찾을 아이템. 아이템 ID(예: diamond, minecraft:golden_apple) 또는 이름 일부(예: '성인의 유해'). 비우면 전체 목록"},
        "include_ender": {"type": "boolean", "description": "엔더상자도 포함할지 (기본 false)"},
    }},
)


def 마크인벤(user_id, player=None, item=None, include_ender=False):
    M = _마크()
    닉, 오류 = _마크닉찾기(user_id, player)
    if 오류:
        return 오류
    내닉 = M.마크연결된닉(user_id)
    if not (내닉 and 내닉.lower() == 닉.lower()) and not _관리자uid(user_id):
        return "다른 사람 인벤토리는 관리자만 볼 수 있습니다. 본인 것이면 '!마크연동 [닉네임]' 으로 먼저 연결해야 합니다."
    try:
        p = M.마크api().player(닉, inventory=True)
    except Exception as e:
        return _마크오류(e)
    if p is None:
        return f"'{닉}' 은 마크 서버에 들어온 적이 없습니다."
    inv = p["inventory"]
    칸 = [("핫바" if it["slot"] < 9 else "가방", it) for it in inv["main"]]
    칸 += [("갑옷", it) for it in inv["armor"].values() if it]
    if inv.get("offhand"):
        칸.append(("왼손", inv["offhand"]))
    if include_ender:
        칸 += [("엔더상자", it) for it in inv["enderChest"]]
    if item:
        q = str(item).strip()
        아이디 = q if ":" in q else "minecraft:" + q
        칸 = [(곳, it) for 곳, it in 칸 if it["id"] == 아이디 or q in it["name"] or q in (it.get("customData") or "")]
        if not 칸:
            return f"{p['name']}: '{q}' 은 없습니다." + ("" if include_ender else " (엔더상자는 안 봤습니다)")

    def 설명(곳, it):
        s = f"[{곳}] {it['name']} x{it['count']} ({it['id']})"
        if it.get("maxDamage"):
            s += f" 내구도 {it['maxDamage'] - it.get('damage', 0)}/{it['maxDamage']}"
        인챈 = it.get("enchantments") or it.get("storedEnchantments")
        if 인챈:
            s += " 인챈트: " + ", ".join(f"{x['name']} {x['level']}" for x in 인챈)
        return s

    줄 = [설명(곳, it) for 곳, it in 칸]
    머리 = f"{p['name']} 인벤토리 ({'접속 중' if p.get('online') else '오프라인, 마지막 저장 기준'})"
    if item:
        머리 += f" — '{item}' 총 {sum(it['count'] for _, it in 칸)}개"
    글 = 머리 + "\n" + "\n".join(줄)
    return 글[:3500] + ("\n... (너무 길어서 잘림)" if len(글) > 3500 else "")


마크연동_선언 = types.FunctionDeclaration(
    name="mc_link_account",
    description=(
        "대화 중인 유저의 디코 계정을 마인크래프트 닉네임과 연결하거나(action=link) 연결을 끊거나(unlink) 확인한다(check). "
        "'내 마크 닉 Steve로 연결해줘' 같은 요청에 쓴다. 서버에 한 번이라도 들어온 닉만 되고, 남이 이미 연결한 닉은 안 된다."
    ),
    parameters={"type": "object", "properties": {
        "action": {"type": "string", "enum": ["link", "unlink", "check"]},
        "name": {"type": "string", "description": "연결할 마크 닉네임 (link 일 때)"},
    }, "required": ["action"]},
)


def 마크연동툴(user_id, action="check", name=None):
    M = _마크()
    if action == "check":
        닉 = M.마크연결된닉(user_id)
        return f"연결된 마크 닉: {닉}" if 닉 else "연결된 마크 닉이 없습니다."
    if action == "unlink":
        return "연결을 끊었습니다." if M.마크연결해제(user_id) else "연결된 닉이 없었습니다."
    닉 = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", 닉):
        return "마크 닉네임은 영문·숫자·_ 16자 이내입니다. 닉네임을 정확히 알려달라고 하세요."
    try:
        p = M.마크api().player(닉, inventory=False)
    except Exception as e:
        return _마크오류(e) + " 서버가 켜져 있을 때 다시 해야 합니다."
    if p is None:
        return f"'{닉}' 은 서버에 들어온 적이 없어 연결할 수 없습니다. 한 번 접속한 뒤에 다시 하라고 하세요."
    닉 = p.get("name") or 닉
    이유 = M.마크연결(user_id, 닉)
    return 이유 or f"마크 닉 '{닉}' 과 연결했습니다."


# ============================================================
# 10-2. 주식 레버리지 (주식.py 레버리지 칸)
# ============================================================

def _레버():
    return 주식


def _정수(x):
    """Gemini 가 8 을 8.0 으로 줄 때가 있다"""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return x


주식주문_선언 = types.FunctionDeclaration(
    name="stock_order",
    description=(
        "현재 대화 중인 유저 본인의 예약 주문(코스피·코스닥만)을 걸거나, 취소하거나, 목록을 본다. "
        "kind: buy = 지정가 매수(현재가가 price 이하가 되면 산다), sell = 지정가 매도(price 이상이 되면 판다), "
        "stop = 손절(price 이하로 떨어지면 바로 판다, 체결가 보장 없음). "
        "지정가는 평균 체결가가 price 를 넘지 않는 만큼만 체결되고 나머지는 계속 기다린다. 장중에만 체결되고 7일 지나면 자동 취소. "
        "체결되면 유저에게 알림이 간다. '삼성 6만원 되면 10주 사줘', '카카오 5만 밑으로 가면 다 팔아줘(손절)', "
        "'내 주문 보여줘', '주문 3번 취소해줘' 같은 요청에 사용한다. 걸기 전에 종목·가격·수량이 분명해야 한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["place", "cancel", "list"], "description": "place 걸기 / cancel 취소 / list 목록"},
            "kind": {"type": "string", "enum": ["buy", "sell", "stop"], "description": "place 일 때 주문 종류"},
            "symbol": {"type": "string", "description": "종목 코드 또는 종목명 (place)"},
            "price": {"type": "string", "description": "가격(원). 숫자 또는 '7만2000' 같은 표기 (place)"},
            "amount": {"type": "string", "description": "수량. sell·stop 은 '전부' 가능 (place)"},
            "order_id": {"type": "string", "description": "취소할 주문 번호 또는 '전부' (cancel)"},
        },
        "required": ["action"],
    },
)


def 주식주문(user_id, action, kind=None, symbol=None, price=None, amount=None, order_id=None, channel_id=None):
    if action == "list":
        목록 = 주식.주문목록(user_id)
        if not 목록:
            return "걸어 둔 예약 주문이 없습니다."
        return "걸린 예약 주문:\n" + "\n".join(
            f"#{o['id']} {o['kindName']} {o['name']}({o['symbol']}) {o['price']:,}원 "
            f"{'전부' if o['qty'] == 0 else str(o['qty']) + '주'}" + (f" (체결 {o['filled']}주)" if o["filled"] else "")
            + f" · {o['expires'][:16].replace('T', ' ')}까지" for o in 목록)
    if action == "cancel":
        성공, r = 주식.주문취소(user_id, str(order_id or ""))
        return f"주문 {r}개를 취소했습니다." if 성공 else r
    if action != "place":
        return "action 은 place, cancel, list 중 하나입니다."
    성공, r = 주식.주문넣기(user_id, str(kind or ""), str(symbol or ""), str(price or ""), str(amount or ""), channel_id)
    if not 성공:
        return r
    return (f"주문 #{r['id']} {r['kindName']} {r['name']}({r['symbol']}) {r['price']:,}원 "
            f"{'전부' if r['qty'] == 0 else str(r['qty']) + '주'}를 걸었습니다. 지금가 {r['now']:,}원."
            + (" 지금 가격이 이미 조건에 맞아 몇 초 안에 체결됩니다." if r["immediate"] else "")
            + f" {r['expires'].replace('T', ' ')}까지 유효.")


주식일정_선언 = types.FunctionDeclaration(
    name="get_stock_events",
    description=(
        "주식의 배당(연 배당률, 다음 배당 기준일, 주당 예상 배당), 실적 발표일, VI(변동성 완화장치)·서킷브레이커 거래 정지 상태, "
        "오늘 거래량을 조회한다. symbol 을 주면 그 종목만, 생략하면 앞으로 2주 실적 발표 일정·배당률 높은 종목·지금 멈춘 종목·거래대금 상위를 준다. "
        "'삼성 배당 얼마야', '다음 실적 발표 언제야', '왜 못 사?(VI)', '오늘 거래량 많은 종목' 같은 질문에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "종목 코드 또는 종목명. 생략하면 전체 요약"}},
    },
)


def 주식일정(symbol=None):
    지금 = datetime.now()
    다음배당 = 주식.다음배당일(지금)
    정지 = 주식.정지목록(cursor, 지금)
    량 = 주식.오늘거래량(cursor)
    if symbol and str(symbol).strip():
        정보 = 시장정보(str(symbol))
        if 정보 is None:
            return f"'{symbol}'은(는) 없는 종목입니다."
        if 정보["market"] == 2:
            return "코인은 배당·실적·VI 가 없습니다."
        if 정보.get("etf"):
            return ETF정보(정보["symbol"])
        s = 정보["symbol"]
        율 = 주식.배당률(s) or 0
        표 = next((x for x in 주식.배당표() if x["symbol"] == s), None)
        줄 = [f"{정보['name']}({s}) 연 배당률 {율 * 100:.2f}%"
             + (f", 다음 기준일 {다음배당:%Y-%m-%d} 장 마감, 예상 주당 {표['dps']:,}원 (배당소득세 {배당세율 * 100:g}%)" if 율 and 표 and 다음배당 else ", 무배당")]
        d = 주식.다음실적(s, 지금)
        줄.append(f"다음 실적 발표 {d:%Y-%m-%d}" + (" (오늘 장중)" if d.date() == 지금.date() else ""))
        줄.append(정지[s]["글"] if s in 정지 else "거래 정지 없음")
        if s in 량:
            줄.append(f"오늘 거래량 {round(량[s][0]):,}주, 거래대금 {량[s][1]:,}원")
        return "\n".join(줄)
    cursor.execute(f"SELECT symbol, name FROM stocks WHERE is_premium IN (0, 1) AND {주식.아님}")
    이름들 = dict(cursor.fetchall())
    일정 = 주식.실적일정(list(이름들), 14, 지금)
    배당 = [x for x in 주식.배당표() if x["yield"] > 0][:8]
    상위 = sorted(((대금, s) for s, (_, 대금) in 량.items() if s in 이름들), reverse=True)[:5]
    줄 = [f"분기 배당 기준일: {'·'.join(f'{m}월' for m in 주식.배당월)} 마지막 거래일 장 마감. 다음 {다음배당:%Y-%m-%d}" if 다음배당 else "",
          "2주 안 실적 발표: " + (", ".join(f"{d:%m/%d} {이름들[s]}" for d, s in 일정) if 일정 else "없음"),
          "배당률 높은 종목: " + ", ".join(f"{x['name']} {x['yield'] * 100:.1f}%" for x in 배당),
          "지금 거래 정지: " + (", ".join(f"{이름들.get(s, s)}({v['종류']})" for s, v in 정지.items()) if 정지 else "없음"),
          "오늘 거래대금 상위: " + (", ".join(f"{이름들[s]} {대금:,}원" for 대금, s in 상위) if 상위 else "없음")]
    return "\n".join(x for x in 줄 if x)


ETF정보_선언 = types.FunctionDeclaration(
    name="get_etf_info",
    description=(
        "ETF(여러 종목을 묶은 상품) 목록이나 ETF 하나의 구성 종목·오늘 기여·배율·총보수를 조회한다. "
        "ETF 는 기초지수(코스피 전 종목, 코스닥 전 종목, 업종) 종목을 같은 비중으로 담고, 레버리지(2배)·인버스(-1배)·곱버스(-2배)는 "
        "하루 등락에 배율을 곱한다 (매일 다시 맞춰서 오래 들고 있으면 출렁이는 장에서 손해가 쌓임). "
        "사고팔기는 buy_stock·sell_stock·stock_order 에 ETF 코드를 그대로 쓰면 된다. 수급 충격·지분 한도 없음, "
        "거래세·양도세 없음, 1배 ETF 만 분기 분배금(배당소득세 15.4%). "
        "'ETF 뭐 있어', '코스피 레버리지 구성 보여줘', '인버스 사면 어떻게 돼' 같은 질문에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "ETF 코드나 이름. 생략하면 전체 ETF 목록"}},
    },
)


def ETF정보(symbol=None):
    전일 = 주식.전일종가들(cursor)

    def 등락(s, p):
        기준 = 전일.get(s)
        return f"{(p - 기준) / 기준 * 100:+.2f}%" if 기준 else "-"

    if symbol and str(symbol).strip():
        정보 = 시장정보(str(symbol))
        if 정보 is None or not 정보.get("etf"):
            return f"'{symbol}'은(는) ETF 가 아닙니다. 생략하면 ETF 목록을 줍니다."
        d = 주식.상세(cursor, 정보["symbol"])
        율 = 주식.배당률(정보["symbol"]) or 0
        줄 = [f"{d['name']}({d['symbol']}) {d['price']:,}원 (전일 대비 {등락(d['symbol'], d['price'])}) — "
             f"{d['index']} {d['count']}종목 동일가중, {d['levName']}, 총보수 연 {d['fee'] * 100:.2f}%, "
             + (f"분배금 연 {율 * 100:.2f}%" if 율 else "분배금 없음") + ", 거래세·양도세 없음",
             f"오늘 구성 종목 평균 {d['indexChange'] * 100:+.2f}% → ETF {d['change'] * 100:+.2f}%"]
        줄.append("구성(오늘 등락): " + ", ".join(f"{h['name']} {h['change'] * 100:+.1f}%" for h in d["holdings"][:30]))
        return "\n".join(줄)
    목록 = 주식.요약(cursor)
    if not 목록:
        return "상장된 ETF 가 없습니다."
    return "ETF 목록:\n" + "\n".join(
        f"{x['name']}({x['symbol']}) {x['price']:,}원 {등락(x['symbol'], x['price'])} — {x['index']} {x['count']}종목, "
        f"{x['levName']}, 총보수 {x['fee'] * 100:.2f}%" for x in 목록)


내세금_선언 = types.FunctionDeclaration(
    name="get_my_stock_tax",
    description=(
        "현재 대화 중인 유저 본인의 올해 주식 세금(실현 손익, 낸 양도세, 거래세, 받은 배당과 배당소득세)을 조회한다. "
        f"규칙: 주식을 팔 때 거래세 {증권거래세 * 100:g}%, 1년 순이익에서 {양도세_공제:,}원을 뺀 만큼의 양도세 {양도세율 * 100:g}%(팔 때마다 정산, 손해 보면 환급), "
        f"배당소득세 {배당세율 * 100:g}%. 코인·레버리지·ETF 매매는 세금 없음(ETF 분배금만 배당소득세). '나 세금 얼마 냈어?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 내세금(user_id):
    t = 세금조회(user_id)
    return (f"{t['year']}년 실현 손익 {t['realized']:+,}원, 양도세 {t['cgt']:,}원, 거래세 {t['trade_tax']:,}원, "
            f"배당 {t['dividend']:,}원(배당소득세 {t['dividend_tax']:,}원). 양도세 없이 더 벌 수 있는 순이익 {t['free_left']:,}원.")


레버리지열기_선언 = types.FunctionDeclaration(
    name="leverage_open",
    description=(
        "현재 대화 중인 유저 본인의 돈으로 주식 레버리지 포지션을 연다. 코스피·코스닥 주식만, 장중에만 된다 (코인 안 됨). "
        "long(롱)은 오르면 이익, short(인버스)는 내리면 이익. "
        "손익 = 증거금 × 배율 × 등락률이고, 손실이 증거금을 다 먹으면 강제 청산되어 증거금을 전부 잃는다 "
        "(2배 -50%, 3배 -33%, 4배 -25% 움직이면). 갭·VI 해제로 한 번에 0 밑까지 떨어지면 모자란 만큼을 현금에서, "
        "현금도 모자라면 대출로 갚는다. 증거금은 포지션 하나에 최대 1억원. "
        "수수료는 포지션 크기(증거금 × 배율)의 0.1%를 열 때와 닫을 때 한 번씩 낸다. "
        "auto_refill 을 켜면 강제 청산 대신 처음 증거금만큼 현금에서 다시 채우고, 현금이 모자라면 대출(원금 합계 1억까지, "
        "10분마다 원금의 2% 이자, 10일 안에 못 갚으면 전 재산 압류)로 채운다. 둘 다 안 되면 그때 청산. "
        "auto_refill 은 유저가 '자동 충전', '자동 대출', '청산되면 대출로 채워줘'처럼 직접 말했을 때만 true 로 한다. "
        "유저가 종목·방향·배율·증거금을 모두 분명히 말했을 때만 쓴다. 빠진 게 있으면 마음대로 정하지 말고 먼저 물어본다. "
        "'삼성 4배 롱 100만원', '네이버 3배 인버스로 전부', '카카오 2배 롱 50만 자동 충전으로' 같은 요청에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드 또는 종목명"},
            "side": {"type": "string", "enum": ["long", "short"], "description": "long = 롱(상승), short = 인버스(하락)"},
            "leverage": {"type": "integer", "description": "배율. 2, 3, 4 중 하나"},
            "margin": {"type": "string", "description": "증거금(원). 숫자, '100만'·'1억 5000만' 같은 표기, 또는 '전부'"},
            "auto_refill": {"type": "boolean", "description": "자동 충전(현금 → 대출). 유저가 직접 원할 때만 true. 기본 false"},
        },
        "required": ["symbol", "side", "leverage", "margin"],
    },
)


def 레버리지열기(user_id, symbol, side, leverage, margin, auto_refill=False, channel_id=None):
    L = _레버()
    방향 = {"long": "long", "롱": "long", "매수": "long", "short": "short", "인버스": "short", "숏": "short"}.get(
        str(side).strip().lower())
    if 방향 is None:
        return "방향은 long(롱) 또는 short(인버스)여야 합니다."
    자동 = auto_refill is True or str(auto_refill).strip().lower() in ("true", "1", "yes")
    성공, r = L.열기(user_id, symbol, 방향, _정수(leverage), str(margin), channel_id, 자동충전=자동)
    if not 성공:
        return r
    return ((f"자동 충전 켜짐: 강제 청산될 때 증거금 {r['margin']:,}원을 현금 → 대출 순으로 다시 채웁니다. " if 자동 else "") +
        f"레버리지 #{r['id']} {r['name']}({r['symbol']}) {r['lev']}배 {L.방향이름[r['side']]}를 열었습니다. "
        f"증거금 {r['margin']:,}원, 포지션 크기 {r['margin'] * r['lev']:,}원, 수수료 {r['fee']:,}원, 진입 현재가 {r['price']:,}원. "
        f"기준가가 {r['liq'] * 100:+.2f}% 움직이면 {'자동 충전' if 자동 else '강제 청산'}됩니다. 남은 잔액 {r['cash']:,}원."
    )


레버리지청산_선언 = types.FunctionDeclaration(
    name="leverage_close",
    description=(
        "현재 대화 중인 유저 본인의 레버리지 포지션을 닫고(청산) 지금 값을 돈으로 받는다. 장중에만 된다. "
        "번호를 모르면 먼저 get_my_leverage 로 확인한다. '레버리지 3번 청산해줘', '레버리지 전부 정리해줘' 같은 요청에 사용한다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "position_id": {"type": "string", "description": "포지션 번호(예: '12') 또는 '전부'"},
        },
        "required": ["position_id"],
    },
)


def 레버리지청산(user_id, position_id):
    L = _레버()
    번호 = _정수(position_id) if str(position_id).strip() not in _전부 else "전부"
    성공, r = L.닫기(user_id, 번호)
    if not 성공:
        return r
    줄 = [f"#{c['id']} {c['name']} {c['lev']}배 {L.방향이름[c['side']]}: 증거금 {c['margin']:,}원 → {c['payout']:,}원 ({c['profit']:+,}원)"
         for c in r["closed"]]
    손익 = sum(c["profit"] for c in r["closed"])
    return "청산했습니다.\n" + "\n".join(줄) + f"\n받은 돈 {r['total']:,}원 (손익 {손익:+,}원), 현재 잔액 {r['cash']:,}원."


내_레버리지_선언 = types.FunctionDeclaration(
    name="get_my_leverage",
    description=(
        "현재 대화 중인 유저 본인의 열린 레버리지 포지션(번호, 종목, 배율, 방향, 증거금, 지금 닫으면 받는 돈, 손익, 강제 청산까지 남은 등락)과 "
        "최근에 닫힌 기록을 조회한다. '내 레버리지 어때?', '레버리지 얼마 벌었어?' 같은 질문에 사용한다."
    ),
    parameters={"type": "object", "properties": {}},
)


def 내_레버리지(user_id):
    L = _레버()
    목록 = L.목록(user_id)
    줄 = [f"열린 포지션 {len(목록)}/{L.최대포지션}개 (주식 장: {주식.장상태()['text']})"]
    for p in 목록:
        줄.append(
            f"#{p['id']} {p['name']}({p['symbol']}) {p['lev']}배 {L.방향이름[p['side']]}: 증거금 {p['margin']:,}원, "
            f"진입 후 기준가 {p['move'] * 100:+.2f}%, 지금 닫으면 {p['value']:,}원 ({p['profit']:+,}원, {p['rate'] * 100:+.1f}%), "
            f"강제 청산까지 {p['liq'] * 100:+.2f}%"
            + (f", 자동 충전 켜짐 (충전 {p['refills']}번, 넣은 돈 합계 {p['invested']:,}원)" if p["auto_refill"] else "")
        )
    기록 = L.기록(user_id, 5)
    if 기록:
        줄.append("최근 닫힌 것:")
        for h in 기록:
            줄.append(f"#{h['id']} {h['name'] or h['symbol']} {h['lev']}배 {L.방향이름.get(h['side'], h['side'])} "
                     f"{h['reason']}: 넣은 돈 {h['margin']:,}원 → {h['payout']:,}원 ({h['payout'] - h['margin']:+,}원)"
                     + (f", 자동 충전 {h['refills']}번" if h.get("refills") else ""))
    return "\n".join(줄)


# ============================================================
# 등록
# ============================================================

모든_선언 = [
    내_잔액조회_선언,
    내_경고조회_선언,
    주가조회_선언,
    주식매수_선언,
    주식매도_선언,
    기억조회_선언,
    픽시브검색_선언,
    내_포트폴리오_선언,
    노래다운_선언,
    마크상태_선언,
    마크플레이어_선언, 마크접속자_선언, 마크전체_선언, 마크인벤_선언, 마크연동_선언,
    기억저장_선언,
    기억불러오기_선언,
    기억삭제_선언,
    대출현황_선언,
    대출하기_선언,
    대출상환_선언,
    채굴현황_선언,
    채굴기상점_선언,
    채굴기구매_선언,
    채굴기판매_선언,
    전환목록_선언,
    사토시전환_선언,
    전기납부_선언,
    전량매도_선언,
    전량매수_선언,
    채굴기전량판매_선언,
    레버리지열기_선언, 레버리지청산_선언, 내_레버리지_선언,
    주식주문_선언, 주식일정_선언, 내세금_선언, ETF정보_선언,
    RPG정보_선언, RPG스탯_선언, RPG상점_선언, RPG구매_선언, RPG판매_선언,
    RPG장착_선언, RPG강화_선언, RPG환전_선언, RPG펫_선언, RPG업적_선언,
    멤버역할_선언,
    음악방송_선언, 노래신청_선언, 노래리스트_선언, 플레이리스트_선언,
    TRPG정보_선언, TRPG시트_선언, TRPG생성_선언, TRPG판정_선언, TRPG공격_선언,
    TRPG피해_선언, TRPG_NPC_선언, TRPG이니시_선언, TRPG치료_선언,
    TRPG과거_선언, TRPG사이버_선언, TRPG넷_선언, TRPG운전_선언, TRPG격투_선언, TRPG연동_선언,
    웹검색_선언, 웹페이지_선언, AI프롬프트_선언,
]

# user_id 를 봇이 직접 주입해야 하는 함수들
USER_ID_필요 = {
    "get_my_balance", "get_my_warnings", "buy_stock", "sell_stock",
    "get_my_portfolio", "remember", "recall", "forget",
    "get_my_loan", "take_loan", "repay_loan",
    "get_mining_status", "buy_mining_rig", "sell_mining_rig",
    "list_coin_conversions", "convert_satoshi", "pay_electricity",
    "sell_all_stocks", "buy_stock_max", "sell_all_mining_rigs",
    "leverage_open", "leverage_close", "get_my_leverage",
    "stock_order", "get_my_stock_tax",
    "rpg_status", "rpg_allocate_stat", "rpg_shop", "rpg_buy", "rpg_sell", "rpg_equip",
    "rpg_enhance", "rpg_gold_exchange", "rpg_pet", "rpg_achievements",
    "get_member_roles",
    "mc_player_info", "mc_player_inventory", "mc_link_account",
    "trpg_sheet", "trpg_create", "trpg_roll", "trpg_attack", "trpg_damage",
    "trpg_npc", "trpg_initiative", "trpg_heal", "trpg_info",
    "trpg_lifepath", "trpg_cyber", "trpg_net", "trpg_drive", "trpg_melee", "trpg_link",
    "music_broadcast", "request_song", "music_queue", "playlist", "ai_prompt",
    "trpg_info", "trpg_sheet", "trpg_create", "trpg_roll", "trpg_attack",
    "trpg_damage", "trpg_npc", "trpg_initiative", "trpg_heal",
    "trpg_lifepath", "trpg_cyber", "trpg_net", "trpg_drive", "trpg_melee", "trpg_link",
}

# 결과에 이미지가 딸려오는 함수들
이미지_반환 = {"search_pixiv", "download_song"}

함수_매핑 = {
    "get_my_balance": 내_잔액조회,
    "get_my_warnings": 내_경고조회,
    "get_stock_price": 주가조회,
    "buy_stock": 주식매수,
    "sell_stock": 주식매도,
    "get_user_memory": 기억조회,
    "search_pixiv": 픽시브검색,
    "get_my_portfolio": 내_포트폴리오,
    "download_song": 노래다운,
    "get_mc_server_status": 마크상태,
    "mc_player_info": 마크플레이어, "mc_online_players": 마크접속자, "mc_all_players": 마크전체,
    "mc_player_inventory": 마크인벤, "mc_link_account": 마크연동툴,
    "remember": 기억저장,
    "recall": 기억불러오기,
    "forget": 기억삭제,
    "get_my_loan": 대출현황,
    "take_loan": 대출하기,
    "repay_loan": 대출상환,
    "get_mining_status": 채굴현황,
    "list_mining_rigs": 채굴기상점,
    "buy_mining_rig": 채굴기구매,
    "sell_mining_rig": 채굴기판매,
    "list_coin_conversions": 전환목록,
    "convert_satoshi": 사토시전환,
    "pay_electricity": 전기납부,
    "sell_all_stocks": 전량매도,
    "buy_stock_max": 전량매수,
    "sell_all_mining_rigs": 채굴기전량판매,
    "leverage_open": 레버리지열기, "leverage_close": 레버리지청산, "get_my_leverage": 내_레버리지,
    "stock_order": 주식주문, "get_stock_events": 주식일정, "get_my_stock_tax": 내세금, "get_etf_info": ETF정보,
    "rpg_status": RPG정보, "rpg_allocate_stat": RPG스탯, "rpg_shop": RPG상점, "rpg_buy": RPG구매,
    "rpg_sell": RPG판매, "rpg_equip": RPG장착, "rpg_enhance": RPG강화, "rpg_gold_exchange": RPG환전,
    "rpg_pet": RPG펫, "rpg_achievements": RPG업적,
    "get_member_roles": 멤버역할,
    "music_broadcast": 음악방송, "request_song": AI노래신청, "music_queue": 노래리스트, "playlist": AI플레이리스트,
    "trpg_info": TRPG정보, "trpg_sheet": TRPG시트, "trpg_create": TRPG생성, "trpg_roll": TRPG판정,
    "trpg_attack": TRPG공격, "trpg_damage": TRPG피해, "trpg_npc": TRPG_NPC,
    "trpg_initiative": TRPG이니시, "trpg_heal": TRPG치료,
    "trpg_lifepath": TRPG과거, "trpg_cyber": TRPG사이버, "trpg_net": TRPG넷,
    "trpg_drive": TRPG운전, "trpg_melee": TRPG격투, "trpg_link": TRPG연동,
    "web_search": 웹검색, "read_webpage": 웹페이지읽기, "ai_prompt": AI프롬프트,
}

# TRPG 는 RPG 점검 스위치와 따로 논다
TRPG_툴 = {"trpg_info", "trpg_sheet", "trpg_create", "trpg_roll", "trpg_attack",
           "trpg_damage", "trpg_npc", "trpg_initiative", "trpg_heal",
           "trpg_lifepath", "trpg_cyber", "trpg_net", "trpg_drive", "trpg_melee", "trpg_link"}


def 실행(이름, args, user_id, channel_id=None):
    """
    봇에서 호출하는 단일 진입점.

    반환값: (텍스트, 첨부파일_dict 또는 None)
    """
    함수 = 함수_매핑.get(이름)
    if 함수 is None:
        return f"알 수 없는 기능입니다: {이름}", None
    if 이름 in 경제_툴 and not 시스템켜짐("경제"):
        return "지금 경제 시스템(돈·주식·채굴·대출)이 점검 중이라 쓸 수 없습니다. 유저에게 그렇게 알려주세요.", None
    if 이름 in RPG_툴 and not 시스템켜짐("RPG"):
        return "지금 RPG가 점검 중이라 쓸 수 없습니다. 유저에게 그렇게 알려주세요.", None
    if 이름 == "rpg_gold_exchange" and not 시스템켜짐("경제"):
        return "지금 경제 시스템이 점검 중이라 골드 환전을 할 수 없습니다.", None

    kwargs = dict(args) if args else {}

    if 이름 in TRPG_툴:
        global 현재채널
        # 넘겨받은 채널을 그대로 쓴다. 안 넘어왔으면 비운다 —
        # 예전 값을 남겨두면 다른 채널의 전투가 섞여 들어온다.
        현재채널 = str(channel_id) if channel_id else None
        import inspect as _inspect
        if "channel_id" in _inspect.signature(함수).parameters:
            kwargs["channel_id"] = 현재채널

    if 이름 in USER_ID_필요:
        kwargs.pop("user_id", None)
        kwargs["user_id"] = user_id
    if 이름 in ("leverage_open", "stock_order"):
        kwargs["channel_id"] = channel_id       # 강제 청산·체결 알림을 보낼 채널 (AI 가 정하지 못하게 덮어쓴다)

    try:
        결과 = 함수(**kwargs)
    except TypeError as e:
        return f"기능 호출에 필요한 값이 부족합니다: {e}", None
    except Exception as e:
        return f"기능 실행 중 오류가 발생했습니다: {e}", None

    if 이름 in 이미지_반환 and isinstance(결과, dict):
        첨부 = None
        if "image" in 결과:
            첨부 = {"data": 결과["image"], "filename": 결과.get("filename", "image.jpg")}
        return 결과["text"], 첨부

    return 결과, None