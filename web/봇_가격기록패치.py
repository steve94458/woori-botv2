# ============================================================
# 봇 파일 패치 — 웹 차트용 가격 기록
#
# 매수 / 매도는 tools.py 안에서 자동으로 기록된다.
# 여기서는 가격이 저절로 바뀌는 두 루프(주가변동, 코인시세갱신)에서
# 기록을 남기도록 몇 줄을 추가한다.
# ============================================================


# ------------------------------------------------------------
# 1. DB 연결 — 웹 서버와 DB를 같이 쓰므로 대기 시간을 늘린다
#    봇 파일 맨 위의 sqlite3.connect 줄을 이렇게 바꾼다
# ------------------------------------------------------------
"""
conn = sqlite3.connect(os.path.join(BASE_DIR, "bot.db"), timeout=15)
conn.execute("PRAGMA journal_mode=WAL")
cursor = conn.cursor()
"""


# ------------------------------------------------------------
# 2. 주가변동 루프 — UPDATE stocks ... 바로 다음 줄에 추가 (주식만)
# ------------------------------------------------------------
"""
            cursor.execute(
                "UPDATE stocks SET price = ?, base_price = ? WHERE symbol = ?",
                (str(새가격), str(새기준가), 코드),
            )
            cursor.execute(                                   # ← 추가
                "INSERT INTO stock_history (symbol, price, ts) VALUES (?, ?, ?)",
                (코드, str(새가격), datetime.now().isoformat(timespec="seconds")),
            )
"""

# 그리고 루프 맨 끝, conn.commit() 바로 다음에 추가 — 한 시간에 한 번 오래된 기록 정리
"""
    conn.commit()

    if datetime.now().minute < 5:                             # ← 추가
        tools.가격기록_정리()
"""


# ------------------------------------------------------------
# 3. 코인시세갱신 루프 — for 항목 in 시세들: 안쪽 UPDATE 다음에 추가
# ------------------------------------------------------------
"""
        cursor.execute(
            "UPDATE stocks SET price = ?, base_price = ?, satoshi_cost = ?, price_updated = ? "
            "WHERE symbol = ?",
            (str(가격), str(가격), str(가격), 지금, 심볼),
        )
        cursor.execute(                                       # ← 추가
            "INSERT INTO stock_history (symbol, price, ts) VALUES (?, ?, ?)",
            (심볼, str(가격), 지금[:19]),
        )
"""
