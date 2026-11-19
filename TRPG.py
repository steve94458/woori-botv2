"""
우리봇v2 TRPG — Cyberpunk 2020 룰 엔진

메인 파일에서는 이렇게만 쓴다:

    import TRPG

    @bot.command(name="TRPG", aliases=["trpg", "티알피지"])
    async def TRPG명령(ctx, 용도: str = None, *인자):
        await TRPG.명령어(ctx, 용도, 인자)

룰 수치는 전부 아래 '데이터' 구역에 모여 있다.
설명 문구는 전부 새로 쓴 것이고, 룰북 본문을 옮겨 적지 않았다.
"""

import json
import os
import random
import sqlite3
import threading
from datetime import datetime

import discord


# ============================================================
# 데이터 — 능력치
# ============================================================

능력치표 = {
    "INT":  ("지능",
             "기억하고 추리하고 알아챈다. 경계·교양·시스템지식 판정의 바탕이고, "
             "넷러닝에서 잠긴 노드를 푸는 데도 쓴다."),
    "REF":  ("반사",
             "몸이 얼마나 빠른가. 전투에서 가장 중요한 능력치다. "
             "명중·회피·이니셔티브·운전이 전부 여기서 나온다. 무거운 방어구를 입으면 떨어진다."),
    "TECH": ("기술",
             "손재주. 기계와 전자기기를 고치고 만든다. "
             "응급처치와 수술, 사이버웨어 설치도 이 능력치로 굴린다."),
    "COOL": ("침착",
             "배짱과 자제력. 겁먹지 않고 버티는 힘이다. "
             "심문에 쓰이고, 인간성이 바닥났을 때 사람으로 남아 있으려면 이게 높아야 한다."),
    "ATTR": ("매력",
             "생김새와 분위기. 무대에 서거나 첫인상으로 상대를 누를 때 쓴다."),
    "LUCK": ("행운",
             "순수한 운. 판정이 아슬아슬할 때 미리 떼어 쓸 수 있는 여유분이다."),
    "MA":   ("이동",
             "한 턴에 얼마나 멀리 가는가. 달리는 거리는 MA × 3 미터다. "
             "엄폐물까지 닿느냐 마느냐가 목숨을 가른다."),
    "BODY": ("체격",
             "힘과 맷집. 받는 피해에서 체격만큼 빼주고, 스턴·데스 세이브의 목표 숫자가 된다. "
             "낮으면 자주 기절하고 자주 죽는다. 들 수 있는 무게도 이걸로 정한다."),
    "EMP":  ("공감",
             "사람과 통하는 능력. 설득·흥정·인맥에 쓴다. "
             "인간성의 최대치(EMP × 10)를 정하고, 사이버웨어를 넣을수록 깎여 내려간다."),
}
능력치순서 = ["INT", "REF", "TECH", "COOL", "ATTR", "LUCK", "MA", "BODY", "EMP"]
능력치_별칭 = {}
for _k, (_ko, _) in 능력치표.items():
    능력치_별칭[_k.lower()] = _k
    능력치_별칭[_ko] = _k

능력치_최소 = 2
능력치_최대 = 10
포인트_기본 = 60          # 영웅적 수준. 50=평범, 70=전설
이름_길이 = (1, 16)


# ============================================================
# 데이터 — 역할 (Role)
# ============================================================
# (이모지, 특수능력, 특수능력 설명, 역할 스킬 9개)

역할표 = {
    "솔로": ("🔫", "전투감각",
        "레벨만큼 이니셔티브와 경계 판정에 더한다",
        ["권총", "소총", "자동화기", "맨손격투", "무기격투", "회피", "운동", "경계", "응급처치"]),
    "넷러너": ("💻", "인터페이스",
        "넷 안에서 하는 모든 행동의 기본 레벨",
        ["전자공학", "시스템지식", "교양", "경계", "회피", "도박", "권총", "설득", "응급처치"]),
    "록커보이": ("🎸", "카리스마",
        "사람을 선동하고 끌어모으는 힘",
        ["악기연주", "작곡", "공연", "설득", "교양", "권총", "맨손격투", "회피", "거리감각"]),
    "솔로테크": ("🔧", "임시수리",
        "부서진 걸 급조해 되살린다",
        ["전자공학", "기계수리", "무기수리", "사이버기술", "교양", "권총", "회피", "경계", "운전"]),
    "메드테크": ("💉", "의료기술",
        "수술과 응급 처치의 숙련도",
        ["응급처치", "진단", "수술", "약학", "생화학", "교양", "권총", "경계", "설득"]),
    "미디어": ("📰", "신뢰도",
        "내 보도를 사람들이 믿어주는 정도",
        ["인터뷰", "작문", "사진", "설득", "교양", "거리감각", "경계", "권총", "회피"]),
    "캅": ("👮", "권위",
        "배지로 상대를 위축시키는 힘",
        ["심문", "경계", "권총", "소총", "맨손격투", "회피", "운전", "법률", "응급처치"]),
    "코퍼": ("💼", "자원",
        "회사에서 끌어다 쓸 수 있는 인력과 장비",
        ["기업지식", "인맥", "설득", "교양", "경계", "회계", "권총", "회피", "운전"]),
    "픽서": ("🕶️", "뒷거래",
        "암시장에서 물건과 정보를 구해온다",
        ["거리감각", "인맥", "흥정", "설득", "경계", "권총", "회피", "위조", "교양"]),
    "노매드": ("🏍️", "패밀리",
        "부족을 불러낼 수 있는 규모",
        ["운전", "정비", "생존", "경계", "소총", "맨손격투", "회피", "응급처치", "거리감각"]),
}

역할_별칭 = {
    "solo": "솔로", "netrunner": "넷러너", "넷러너": "넷러너", "네트러너": "넷러너",
    "rockerboy": "록커보이", "록커": "록커보이", "techie": "솔로테크", "테크": "솔로테크",
    "테키": "솔로테크", "medtech": "메드테크", "메딕": "메드테크",
    "media": "미디어", "기자": "미디어", "cop": "캅", "경찰": "캅",
    "corporate": "코퍼", "코퍼레이트": "코퍼", "기업인": "코퍼",
    "fixer": "픽서", "nomad": "노매드", "유랑민": "노매드",
}

# ============================================================
# 스킬 — 어떤 능력치로 굴리고 무엇에 쓰는지
# ============================================================
# 이름: (쓰는 능력치, 언제 굴리는지)

스킬표 = {
    # --- 전투 ---
    "권총":       ("REF", "권총과 기관권총을 쏜다. 숨기기 쉬워 거리에서 제일 흔한 무기다"),
    "소총":       ("REF", "소총과 산탄총을 쏜다. 멀리서 확실하게 죽이고 싶을 때"),
    "자동화기":   ("REF", "기관단총·기관총으로 연사한다. 한 번에 여러 발을 박아 넣는다"),
    "중화기":     ("REF", "대물소총·유탄발사기 같은 과한 물건을 다룬다"),
    "맨손격투":   ("REF", "주먹과 발로 싸운다. 유파를 배우면 무술 스킬로 넘어간다"),
    "무기격투":   ("REF", "칼·둔기·카타나처럼 손에 든 걸로 때린다"),
    "무술":       ("REF", "배운 유파로 싸운다. 기술마다 유파 보너스가 다르게 붙는다"),
    "회피":       ("REF", "날아오는 공격을 피한다. 전투에서 제일 자주 굴리는 방어 스킬"),
    "운동":       ("REF", "뛰고 오르고 뛰어넘는다. 도망칠 때 목숨이 걸린다"),
    # --- 감각·판단 ---
    "경계":       ("INT", "숨은 적, 함정, 미행을 알아챈다. 매복당하지 않으려면 이게 필요하다"),
    "교양":       ("INT", "세상 돌아가는 일반 지식. 모르는 것에 대해 아는 게 있나 굴린다"),
    "거리감각":   ("INT", "뒷골목의 규칙과 소문을 안다. 누구한테 물어야 할지 아는 능력"),
    "법률":       ("INT", "법과 절차를 안다. 체포당했을 때, 계약서를 읽을 때"),
    "기업지식":   ("INT", "어느 기업이 뭘 하고 누가 실세인지 안다"),
    "회계":       ("INT", "장부를 읽고 돈의 흐름을 쫓는다. 횡령을 찾아낼 때"),
    "도박":       ("INT", "카드와 주사위. 속임수를 쓰거나 남의 속임수를 잡아낸다"),
    "생존":       ("INT", "도시 밖에서 먹고 자고 길을 찾는다. 노매드의 생명줄"),
    # --- 사람 다루기 ---
    "설득":       ("EMP", "말로 상대를 움직인다. 협상, 부탁, 거짓말 전부"),
    "심문":       ("COOL", "상대에게서 정보를 끌어낸다. 압박하거나 구슬린다"),
    "흥정":       ("EMP", "값을 깎고 거래를 성사시킨다. 픽서의 밥줄"),
    "인맥":       ("EMP", "아는 사람을 통해 물건·정보·사람을 구한다"),
    "인터뷰":     ("EMP", "상대가 말하게 만든다. 기자의 기본기"),
    "공연":       ("ATTR", "무대에서 사람을 사로잡는다"),
    "악기연주":   ("TECH", "악기를 다룬다"),
    "작곡":       ("INT", "곡을 쓴다. 사람 마음을 흔드는 노래일수록 높게 굴린다"),
    "작문":       ("INT", "글을 쓴다. 기사, 선전물, 가짜 보고서"),
    "사진":       ("TECH", "결정적 장면을 찍는다. 증거가 되기도 한다"),
    # --- 기술 ---
    "전자공학":   ("TECH", "전자기기를 고치고 개조한다. 보안 장치를 뜯을 때도 쓴다"),
    "기계수리":   ("TECH", "기계를 고친다. 문, 엘리베이터, 공장 설비"),
    "무기수리":   ("TECH", "총을 정비하고 개조한다. 걸린 총을 살릴 때"),
    "사이버기술": ("TECH", "사이버웨어를 설치하고 수리한다"),
    "정비":       ("TECH", "차량을 손본다. 달리는 중에 고치기도 한다"),
    "위조":       ("TECH", "서류와 신분증을 가짜로 만든다"),
    "운전":       ("REF", "차·오토바이를 몬다. 추격과 도주에 굴린다"),
    "시스템지식": ("INT", "컴퓨터 시스템의 구조를 안다. 어디를 노려야 할지 파악한다"),
    "인터페이스": ("INT", "넷에 접속해 움직인다. 넷러너의 핵심 능력"),
    # --- 의료 ---
    "응급처치":   ("TECH", "피를 멎게 하고 숨을 붙여둔다. 전투 직후 제일 급한 것"),
    "진단":       ("TECH", "무슨 병인지, 어디가 망가졌는지 알아낸다"),
    "수술":       ("TECH", "몸을 열고 고친다. 사이버웨어 설치도 여기 들어간다"),
    "약학":       ("TECH", "약을 조제하고 효과와 부작용을 안다"),
    "생화학":     ("INT", "독, 약물, 생체 물질을 분석한다"),
    "의지":       ("COOL", "공포와 충동을 억누른다. 인간성이 바닥났을 때 버티는 힘"),
}

역할스킬_포인트 = 40        # 역할 스킬 9개 + 특수능력에 나눠 쓰는 점수
일반스킬_포인트 = None       # None 이면 INT+REF 합으로 계산
스킬_최대 = 10


# ============================================================
# 데이터 — 판정
# ============================================================

난이도표 = {
    "쉬움": 10, "보통": 15, "어려움": 20, "매우어려움": 25, "거의불가능": 30,
}
난이도_별칭 = {
    "easy": "쉬움", "average": "보통", "normal": "보통", "평범": "보통",
    "difficult": "어려움", "hard": "어려움",
    "very": "매우어려움", "veryhard": "매우어려움", "매우": "매우어려움",
    "impossible": "거의불가능", "불가능": "거의불가능",
}


# ============================================================
# 데이터 — 전투
# ============================================================

부위표 = [  # (1D10 범위, 이름, 방어구 칸)
    ((1, 1),  "머리",     "head"),
    ((2, 4),  "몸통",     "torso"),
    ((5, 5),  "오른팔",   "arm"),
    ((6, 6),  "왼팔",     "arm"),
    ((7, 8),  "오른다리", "leg"),
    ((9, 10), "왼다리",   "leg"),
]
머리_배수 = 2

사거리표 = {   # 이름: (명중 목표치, 설명)
    "밀착":   (10, "총구가 닿을 거리"),
    "근거리": (15, "무기 사거리의 1/4 이내"),
    "중거리": (20, "무기 사거리의 1/2 이내"),
    "원거리": (25, "무기 사거리까지"),
    "극원거리": (30, "무기 사거리의 2배"),
}
사거리_별칭 = {
    "pb": "밀착", "점사": "밀착", "밀착": "밀착",
    "close": "근거리", "가까움": "근거리", "근": "근거리",
    "medium": "중거리", "중": "중거리",
    "long": "원거리", "원": "원거리",
    "extreme": "극원거리", "극": "극원거리",
}

# 체격(BODY) → (분류, BTM). BTM 은 받은 피해에서 빼는 값
체격표 = [
    (2,  "매우 약함", 0),
    (4,  "약함",      -1),
    (7,  "보통",      -2),
    (9,  "강함",      -3),
    (10, "매우 강함", -4),
    (12, "초인",      -5),
    (99, "괴물",      -6),
]

# 누적 피해 → (상태 이름, 스턴 페널티, 치명 단계)
부상표 = [
    (0,  "멀쩡함",   0,  None),
    (4,  "경상",     0,  None),
    (8,  "중상",     -1, None),
    (12, "위독",     -2, None),
    (16, "치명 0",   -3, 0),
    (20, "치명 1",   -4, 1),
    (24, "치명 2",   -5, 2),
    (28, "치명 3",   -6, 3),
    (32, "치명 4",   -7, 4),
    (36, "치명 5",   -8, 5),
    (40, "치명 6",   -9, 6),
]

# 부상 상태별 능력치 페널티 (REF, INT, COOL 에 적용)
부상_페널티 = {"멀쩡함": 0, "경상": 0, "중상": -2, "위독": -4}
치명_페널티 = -6


# ============================================================
# 데이터 — 장비
# ============================================================
# 무기: (분류, 스킬, 명중보정, 피해, 연사, 사거리m, 가격eb, 설명)

무기 = {
    # 권총
    "경권총":       ("권총", "권총", -1, "1D6+1",  2,  50,   150, "숨기기 좋은 소형 자동권총"),
    "중권총":       ("권총", "권총",  0, "2D6+1",  2,  50,   350, "9mm. 가장 무난한 자동권총"),
    "대구경권총":   ("권총", "권총",  0, "2D6+3",  2,  50,   400, "10mm. 거리와 위력이 좋다"),
    "중권총(강)":   ("권총", "권총",  0, "3D6",    2,  50,   500, "11mm. 제압력이 뛰어나다"),
    "초대형권총":   ("권총", "권총",  0, "4D6+1",  1,  50,   550, "12mm. 반동이 크지만 한 방이 무겁다"),
    # 기관단총
    "경기관단총":   ("기관단총", "자동화기", 1, "2D6+1", 35, 150,  500, "9mm 소형 SMG"),
    "중기관단총":   ("기관단총", "자동화기", 0, "2D6+3", 20, 150,  600, "10mm. 경비용 표준"),
    "대형기관단총": ("기관단총", "자동화기", 0, "4D6+1", 20, 150,  900, "12mm. 솔로들이 가장 많이 쓴다"),
    # 소총
    "경돌격소총":   ("소총", "소총", 1, "5D6",    30, 400, 1000, "5.56mm 경량 돌격소총"),
    "중돌격소총":   ("소총", "소총", 0, "5D6",    30, 400,  800, "5.56mm. 튼튼하고 흔하다"),
    "중화기소총":   ("소총", "소총", -1, "6D6+2", 30, 400, 1200, "7.62mm. 전장용 주력"),
    # 산탄총
    "전투산탄총":   ("산탄총", "소총", -1, "4D6",  10,  50,  500, "근거리에서 가장 잔혹하다"),
    "경산탄총":     ("산탄총", "소총", -2, "4D6",   2,  50,  350, "경찰용 단축형"),
    # 중화기
    "대물소총":     ("중화기", "중화기", 0, "4D10", 1, 1000, 3000, "20mm. 사이버사이코 사냥용"),
    # 근접
    "주먹":         ("근접", "맨손격투", 0, "1D6",  1, 1, 0, "맨손"),
    "곤봉":         ("근접", "무기격투", 0, "1D6",  1, 1, 20, "흔한 둔기"),
    "나이프":       ("근접", "무기격투", 0, "1D6",  1, 1, 50, "숨기기 쉽다"),
    "검":           ("근접", "무기격투", 0, "2D6",  1, 1, 200, "긴 날붙이"),
    "모노나이프":   ("근접", "무기격투", 1, "2D6",  1, 1, 200, "단분자 날. 방어구를 잘 썬다"),
    "모노카타나":   ("근접", "무기격투", 1, "4D6",  1, 1, 600, "단분자 일본도. 사람을 두 동강 낸다"),
    "배틀글러브":   ("근접", "무기격투", -2, "3D6", 1, 1, 900, "강화 장갑. 둔하지만 묵직하다"),
}
무기_별칭 = {"맨손": "주먹", "fist": "주먹"}

# 방어구: (SP, 덮는 부위 리스트, 가격eb, 설명)
방어구 = {
    "가죽재킷":     (4,  ["torso", "arm"],                 50,  "없는 것보단 낫다"),
    "방탄티":       (4,  ["torso"],                        70,  "티셔츠처럼 입는다"),
    "케블라조끼":   (10, ["torso"],                        150, "흔한 방탄조끼"),
    "경장갑재킷":   (14, ["torso", "arm"],                 150, "가벼운 장갑 재킷"),
    "장갑재킷":     (16, ["torso", "arm"],                 200, "거리의 표준. 가성비가 좋다"),
    "중장갑재킷":   (18, ["torso", "arm"],                 250, "무겁지만 든든하다"),
    "방탄복":       (20, ["torso"],                        200, "군용 플랙 베스트"),
    "메탈기어":     (25, ["head", "torso", "arm", "leg"],  600, "전신 장갑. REF -2"),
    "헬멧":         (14, ["head"],                         100, "머리만 지킨다"),
    "중헬멧":       (20, ["head"],                         200, "군용 방탄모"),
}
방어구_REF페널티 = {"메탈기어": -2, "중장갑재킷": -1}

아이템 = {
    "응급키트":   (50,  "응급처치 판정에 +2"),
    "수술키트":   (500, "수술·안정화 판정에 +2"),
    "스피드힐":   (100, "즉시 피해 1D6 회복 (전투당 1회)"),
    "탄창":       (20,  "예비 탄창"),
    "사이버덱":   (1000, "넷러닝에 필요"),
}

시작_유로달러 = 1000


# ============================================================
# DB
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(BASE_DIR, "bot.db")
_로컬 = threading.local()


def _연결():
    """
    sqlite 커서 객체는 여러 스레드가 같이 쓰면 터진다.
    그래서 스레드마다 자기 연결과 커서를 따로 갖는다.
    (웹 서버는 동기 엔드포인트를 스레드풀에서 돌린다)
    """
    c = getattr(_로컬, "conn", None)
    if c is None:
        c = sqlite3.connect(_DB, check_same_thread=False, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        _로컬.conn = c
        _로컬.cur = c.cursor()
    return _로컬.conn, _로컬.cur


class _연결프록시:
    """conn 처럼 보이지만 실제로는 지금 스레드의 연결로 넘겨준다"""
    def __getattr__(self, 이름):
        return getattr(_연결()[0], 이름)


class _커서프록시:
    def __getattr__(self, 이름):
        return getattr(_연결()[1], 이름)

    def __iter__(self):
        return iter(_연결()[1])


conn = _연결프록시()
cursor = _커서프록시()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS trpg_characters (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        role TEXT,
        stat_int INTEGER DEFAULT 5, stat_ref INTEGER DEFAULT 5, stat_tech INTEGER DEFAULT 5,
        stat_cool INTEGER DEFAULT 5, stat_attr INTEGER DEFAULT 5, stat_luck INTEGER DEFAULT 5,
        stat_ma INTEGER DEFAULT 5, stat_body INTEGER DEFAULT 5, stat_emp INTEGER DEFAULT 5,
        special INTEGER DEFAULT 1,
        damage INTEGER DEFAULT 0,
        humanity_lost INTEGER DEFAULT 0,
        money INTEGER DEFAULT 0,
        weapon TEXT, armor TEXT,
        lifepath TEXT,
        created_at TEXT
    )
""")
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_skills (user_id TEXT, name TEXT, level INTEGER DEFAULT 0, PRIMARY KEY (user_id, name))")
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_gear (user_id TEXT, kind TEXT, name TEXT, qty INTEGER DEFAULT 1, PRIMARY KEY (user_id, kind, name))")
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_npcs (channel_id TEXT, name TEXT, body INTEGER, ref INTEGER, skill INTEGER, armor INTEGER, damage INTEGER DEFAULT 0, weapon TEXT, PRIMARY KEY (channel_id, name))")
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_initiative (channel_id TEXT, name TEXT, roll INTEGER, PRIMARY KEY (channel_id, name))")
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_net (channel_id TEXT, name TEXT, data TEXT, PRIMARY KEY (channel_id, name))")

for _sql in (
    "ALTER TABLE trpg_characters ADD COLUMN linked INTEGER DEFAULT 0",
    "ALTER TABLE trpg_characters ADD COLUMN emp_base INTEGER DEFAULT 0",
):
    try:
        cursor.execute(_sql)
    except sqlite3.OperationalError:
        pass
# 시작 유로달러는 처음 만들 때 한 번만 준다 (지웠다 다시 만들어서 eb→골드로 돈을 복사하지 못하게)
cursor.execute("CREATE TABLE IF NOT EXISTS trpg_history (user_id TEXT PRIMARY KEY)")
cursor.execute("INSERT OR IGNORE INTO trpg_history (user_id) SELECT user_id FROM trpg_characters")
conn.commit()


def _지금():
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# 주사위
# ============================================================

def d10():
    return random.randint(1, 10)


def 굴림(식):
    """'2D6+3' 같은 식을 굴려 (합계, [눈들], 보정) 반환"""
    식 = str(식).upper().replace(" ", "")
    보정 = 0
    if "+" in 식:
        식, 더함 = 식.split("+", 1)
        보정 = int(더함)
    elif "-" in 식[1:]:
        i = 식.index("-", 1)
        보정 = -int(식[i + 1:])
        식 = 식[:i]
    개수, _, 면 = 식.partition("D")
    개수 = int(개수 or 1)
    면 = int(면 or 6)
    눈 = [random.randint(1, 면) for _ in range(개수)]
    return sum(눈) + 보정, 눈, 보정


def 판정주사위():
    """
    CP2020 판정 주사위.
    10이 나오면 한 번 더 굴려 더하고, 1이 나오면 한 번 더 굴려 뺀다.
    반환: (최종값, 눈 기록 문자열, 대성공여부, 펌블여부)
    """
    첫 = d10()
    if 첫 == 10:
        추가 = d10()
        return 10 + 추가, f"10💥+{추가}", True, False
    if 첫 == 1:
        추가 = d10()
        return 1 - 추가, f"1💀-{추가}", False, True
    return 첫, str(첫), False, False


# ============================================================
# 캐릭터
# ============================================================

def 캐릭터(user_id):
    cursor.execute("SELECT * FROM trpg_characters WHERE user_id = ?", (str(user_id),))
    r = cursor.fetchone()
    if r is None:
        return None
    컬럼 = [d[0] for d in cursor.description]
    c = dict(zip(컬럼, r))
    c["_uid"] = str(user_id)
    c["스킬"] = 스킬목록(user_id)
    return c


def 스킬목록(user_id):
    cursor.execute("SELECT name, level FROM trpg_skills WHERE user_id = ? ORDER BY level DESC, name", (str(user_id),))
    return {n: lv for n, lv in cursor.fetchall()}


def 능력(c, 키):
    """부상 페널티와 사이버웨어까지 반영한 현재 능력치"""
    기본 = c[f"stat_{키.lower()}"]
    if 키 in ("REF", "INT", "COOL"):
        기본 += 부상페널티(c["damage"])
    if 키 == "REF":
        기본 += 방어구_REF페널티.get(c.get("armor") or "", 0)
    보너스 = 사이버보너스(c["_uid"]) if c.get("_uid") else {}
    기본 += 보너스.get(키, 0)
    return max(0, 기본)


def 체격정보(body):
    for 상한, 이름, btm in 체격표:
        if body <= 상한:
            return 이름, btm
    return 체격표[-1][1], 체격표[-1][2]


def 부상정보(피해):
    상태, 스턴, 치명 = "멀쩡함", 0, None
    for 상한, 이름, s, m in 부상표:
        if 피해 > 상한:
            continue
        상태, 스턴, 치명 = 이름, s, m
        break
    else:
        상태, 스턴, 치명 = "사망", -10, 99
    return 상태, 스턴, 치명


def 부상페널티(피해):
    상태, _, 치명 = 부상정보(피해)
    if 치명 is not None:
        return 치명_페널티
    return 부상_페널티.get(상태, 0)


def EMP기준(c):
    """사이버웨어를 달기 전의 원래 EMP. 인간성의 최대치를 정한다"""
    v = c.get("emp_base") or 0
    return v if v > 0 else c["stat_emp"]


def 인간성(c):
    return max(0, EMP기준(c) * 10 - c["humanity_lost"])


def 최대인간성(c):
    return EMP기준(c) * 10


def 생성(user_id, 이름, 역할, 능력치=None, 포인트=포인트_기본):
    user_id = str(user_id)
    if 캐릭터(user_id) is not None:
        return False, "이미 캐릭터가 있습니다. `!TRPG 삭제` 로 지우고 다시 만들 수 있습니다."

    최소, 최대 = 이름_길이
    이름 = (이름 or "").strip()
    if not (최소 <= len(이름) <= 최대):
        return False, f"이름은 {최소}~{최대}자여야 합니다."

    역할 = 역할_별칭.get(str(역할 or "").lower(), 역할)
    if 역할 not in 역할표:
        return False, "역할을 고르세요: " + ", ".join(역할표)

    if 능력치 is None:
        능력치 = 능력치_랜덤(포인트)
    else:
        ok, r = 능력치_검사(능력치, 포인트)
        if not ok:
            return False, r
        능력치 = r

    _, 특수, _, 역할스킬 = 역할표[역할]
    cursor.execute("INSERT OR IGNORE INTO trpg_history (user_id) VALUES (?)", (user_id,))
    시작돈 = 시작_유로달러 if cursor.rowcount > 0 else 0
    cursor.execute(
        "INSERT INTO trpg_characters (user_id, name, role, stat_int, stat_ref, stat_tech, stat_cool, "
        "stat_attr, stat_luck, stat_ma, stat_body, stat_emp, special, money, weapon, armor, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, 이름, 역할, 능력치["INT"], 능력치["REF"], 능력치["TECH"], 능력치["COOL"],
         능력치["ATTR"], 능력치["LUCK"], 능력치["MA"], 능력치["BODY"], 능력치["EMP"],
         4, 시작돈, "중권총", None, _지금()),
    )
    cursor.execute("UPDATE trpg_characters SET emp_base = ? WHERE user_id = ?", (능력치["EMP"], user_id))
    # 역할 스킬을 자동으로 배분한다
    남은 = 역할스킬_포인트
    배분 = {}
    for i, s in enumerate(역할스킬):
        몫 = max(1, min(스킬_최대, 남은 // (len(역할스킬) - i)))
        배분[s] = 몫
        남은 -= 몫
    for s, lv in 배분.items():
        cursor.execute("INSERT OR REPLACE INTO trpg_skills (user_id, name, level) VALUES (?, ?, ?)", (user_id, s, lv))
    cursor.execute("INSERT OR REPLACE INTO trpg_gear (user_id, kind, name, qty) VALUES (?, 'weapon', '중권총', 1)", (user_id,))
    conn.commit()
    return True, 캐릭터(user_id)


def 능력치_랜덤(포인트=포인트_기본):
    """포인트 총합을 지키면서 무작위로 9개 능력치를 나눈다"""
    값 = {k: 능력치_최소 for k in 능력치순서}
    남은 = 포인트 - 능력치_최소 * len(능력치순서)
    후보 = list(능력치순서)
    while 남은 > 0 and 후보:
        k = random.choice(후보)
        값[k] += 1
        남은 -= 1
        if 값[k] >= 능력치_최대:
            후보.remove(k)
    return 값


def 능력치_검사(입력, 포인트=포인트_기본):
    값 = {}
    for k in 능력치순서:
        v = 입력.get(k, 입력.get(k.lower()))
        if v is None:
            return False, f"{k} 값이 없습니다."
        try:
            v = int(v)
        except (TypeError, ValueError):
            return False, f"{k} 는 숫자여야 합니다."
        if not (능력치_최소 <= v <= 능력치_최대):
            return False, f"{k} 는 {능력치_최소}~{능력치_최대} 사이여야 합니다."
        값[k] = v
    합 = sum(값.values())
    if 합 > 포인트:
        return False, f"능력치 합이 {합}입니다. {포인트} 이하로 맞춰주세요."
    return True, 값


def 삭제(user_id):
    user_id = str(user_id)
    if 캐릭터(user_id) is None:
        return False, "캐릭터가 없습니다."
    for t in ("trpg_characters", "trpg_skills", "trpg_gear"):
        cursor.execute(f"DELETE FROM {t} WHERE user_id = ?", (user_id,))
    conn.commit()
    return True, "캐릭터를 지웠습니다."


def 스킬설정(user_id, 이름, 레벨):
    try:
        레벨 = int(레벨)
    except (TypeError, ValueError):
        return False, "레벨은 숫자여야 합니다."
    if not (0 <= 레벨 <= 스킬_최대):
        return False, f"스킬 레벨은 0~{스킬_최대} 입니다."
    if 레벨 == 0:
        cursor.execute("DELETE FROM trpg_skills WHERE user_id = ? AND name = ?", (str(user_id), 이름))
    else:
        cursor.execute("INSERT OR REPLACE INTO trpg_skills (user_id, name, level) VALUES (?, ?, ?)",
                       (str(user_id), 이름, 레벨))
    conn.commit()
    return True, f"{이름} 을(를) Lv.{레벨} 로 설정했습니다."


# ============================================================
# 판정
# ============================================================

def 난이도값(난이도):
    if 난이도 is None:
        return 난이도표["보통"], "보통"
    s = str(난이도).strip()
    if s.isdigit():
        n = int(s)
        for 이름, v in 난이도표.items():
            if v == n:
                return n, 이름
        return n, f"DV {n}"
    키 = 난이도_별칭.get(s.lower(), s)
    if 키 in 난이도표:
        return 난이도표[키], 키
    return 난이도표["보통"], "보통"


def 판정(user_id, 능력치키, 스킬=None, 난이도=None, 보정=0):
    """
    반환: dict(성공, 총합, 목표, 주사위, 능력, 스킬레벨, 보정, 대성공, 펌블, 문장)
    """
    c = 캐릭터(user_id)
    if c is None:
        return None, "캐릭터가 없습니다. `!TRPG 생성` 으로 만들어주세요."

    키 = 능력치_별칭.get(str(능력치키 or "").lower(), str(능력치키 or "").upper())
    if 키 not in 능력치표:
        return None, "능력치를 찾을 수 없습니다: " + ", ".join(능력치순서)

    스킬레벨 = 0
    if 스킬:
        스킬레벨 = c["스킬"].get(스킬, 0)

    목표, 난이도이름 = 난이도값(난이도)
    눈, 기록, 대성공, 펌블 = 판정주사위()
    능력값 = 능력(c, 키)
    try:
        보정 = int(보정 or 0)
    except (TypeError, ValueError):
        보정 = 0
    총합 = 눈 + 능력값 + 스킬레벨 + 보정
    성공 = 총합 >= 목표

    조각 = [f"1D10({기록})", f"{키} {능력값}"]
    if 스킬:
        조각.append(f"{스킬} {스킬레벨}")
    if 보정:
        조각.append(f"보정 {보정:+d}")

    return {
        "성공": 성공, "총합": 총합, "목표": 목표, "난이도": 난이도이름,
        "주사위": 눈, "기록": 기록, "능력": 능력값, "능력치": 키,
        "스킬": 스킬, "스킬레벨": 스킬레벨, "보정": 보정,
        "대성공": 대성공, "펌블": 펌블, "이름": c["name"],
        "식": " + ".join(조각),
    }, None


# ============================================================
# 전투 — 이니셔티브
# ============================================================

def 이니시추가(channel_id, 이름, 굴린값):
    cursor.execute("INSERT OR REPLACE INTO trpg_initiative (channel_id, name, roll) VALUES (?, ?, ?)",
                   (str(channel_id), 이름, int(굴린값)))
    conn.commit()


def 이니시굴림(channel_id, user_id=None, 이름=None, REF=None, 보정=0):
    """캐릭터면 user_id, NPC면 이름+REF"""
    if user_id is not None:
        c = 캐릭터(user_id)
        if c is None:
            return None, "캐릭터가 없습니다."
        이름 = c["name"]
        ref = 능력(c, "REF")
        if c["role"] == "솔로":
            보정 += c["special"]          # 전투감각
    else:
        if not 이름:
            return None, "이름이 필요합니다."
        ref = int(REF or 5)
    눈 = d10()
    값 = 눈 + ref + int(보정 or 0)
    이니시추가(channel_id, 이름, 값)
    return {"이름": 이름, "주사위": 눈, "REF": ref, "보정": 보정, "값": 값}, None


def 이니시목록(channel_id):
    cursor.execute("SELECT name, roll FROM trpg_initiative WHERE channel_id = ? ORDER BY roll DESC, name",
                   (str(channel_id),))
    return cursor.fetchall()


def 이니시초기화(channel_id):
    cursor.execute("DELETE FROM trpg_initiative WHERE channel_id = ?", (str(channel_id),))
    conn.commit()
    return "이니셔티브를 비웠습니다."


# ============================================================
# 전투 — NPC
# ============================================================

def NPC추가(channel_id, 이름, body=5, ref=5, skill=4, armor=0, weapon="중권총"):
    cursor.execute(
        "INSERT OR REPLACE INTO trpg_npcs (channel_id, name, body, ref, skill, armor, damage, weapon) "
        "VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT damage FROM trpg_npcs WHERE channel_id=? AND name=?), 0), ?)",
        (str(channel_id), 이름, int(body), int(ref), int(skill), int(armor), str(channel_id), 이름, weapon),
    )
    conn.commit()
    return f"{이름} 추가 (BODY {body} / REF {ref} / 스킬 {skill} / 방어 SP {armor})"


def NPC(channel_id, 이름):
    cursor.execute("SELECT name, body, ref, skill, armor, damage, weapon FROM trpg_npcs WHERE channel_id = ? AND name = ?",
                   (str(channel_id), 이름))
    r = cursor.fetchone()
    if r is None:
        return None
    return dict(zip(["name", "body", "ref", "skill", "armor", "damage", "weapon"], r))


def NPC목록(channel_id):
    cursor.execute("SELECT name, body, ref, skill, armor, damage FROM trpg_npcs WHERE channel_id = ?", (str(channel_id),))
    return cursor.fetchall()


def NPC정리(channel_id):
    cursor.execute("DELETE FROM trpg_npcs WHERE channel_id = ?", (str(channel_id),))
    conn.commit()
    return "NPC를 모두 지웠습니다."


# ============================================================
# 전투 — 공격과 피해
# ============================================================

def 부위판정(눈=None):
    눈 = 눈 or d10()
    for (lo, hi), 이름, 칸 in 부위표:
        if lo <= 눈 <= hi:
            return 눈, 이름, 칸
    return 눈, "몸통", "torso"


def 방어SP(대상, 칸):
    """대상이 캐릭터 dict 면 입은 방어구 + 피하 장갑, NPC면 통짜 SP"""
    if "_uid" in 대상:
        sp = 0
        이름 = 대상.get("armor")
        if 이름 and 이름 in 방어구:
            칸들 = 방어구[이름][1]
            if 칸 in 칸들:
                sp = 방어구[이름][0]
        피하 = 사이버보너스(대상["_uid"]).get("SP", 0)
        if 피하:
            # 피하 장갑과 겉 방어구는 겹쳐 입은 것으로 본다 (높은 쪽 + 차이에 따른 보너스)
            높은, 낮은 = max(sp, 피하), min(sp, 피하)
            차 = 높은 - 낮은
            보너스 = 5 if 차 <= 4 else 4 if 차 <= 8 else 3 if 차 <= 14 else 2 if 차 <= 20 else 1 if 차 <= 26 else 0
            sp = 높은 + (보너스 if 낮은 else 0)
        return sp
    if "armor" in 대상 and isinstance(대상.get("armor"), str):
        이름 = 대상["armor"]
        if 이름 in 방어구:
            sp, 부위들, _, _ = 방어구[이름]
            return sp if 칸 in 부위들 else 0
        return 0
    return int(대상.get("armor") or 0)


def 피해계산(대상, 피해량, 칸, 부위이름, AP=False):
    """반환: (실제피해, 설명줄)"""
    sp = 방어SP(대상, 칸)
    if AP:
        sp = sp // 2
        피해량 = max(1, 피해량 // 2)
    뚫은뒤 = 피해량 - sp
    if 뚫은뒤 <= 0:
        return 0, f"{부위이름} 명중 — 방어구 SP {sp} 가 {피해량} 피해를 전부 막았다"

    if 부위이름 == "머리":
        뚫은뒤 *= 머리_배수

    body = 대상.get("stat_body", 대상.get("body", 5))
    _, btm = 체격정보(body)
    실제 = max(1, 뚫은뒤 + btm)      # btm 은 음수

    줄 = f"{부위이름} 명중 — 피해 {피해량} − SP {sp}"
    if 부위이름 == "머리":
        줄 += f" ×{머리_배수}(머리)"
    줄 += f" − 체격 {abs(btm)} = **{실제}**"
    return 실제, 줄


def 공격(channel_id, 공격자_uid=None, 공격자_npc=None, 대상=None,
         무기이름=None, 사거리="중거리", 보정=0, 연사=1):
    """
    공격자는 user_id 또는 NPC 이름. 대상은 캐릭터 user_id 또는 NPC 이름.
    반환: (결과 dict, 오류문자열)
    """
    # ---- 공격자 ----
    if 공격자_uid is not None:
        c = 캐릭터(공격자_uid)
        if c is None:
            return None, "공격자 캐릭터가 없습니다."
        공격자이름 = c["name"]
        ref = 능력(c, "REF")
        무기이름 = 무기이름 or c["weapon"] or "주먹"
        무기이름 = 무기_별칭.get(무기이름, 무기이름)
        if 무기이름 not in 무기:
            return None, f"'{무기이름}' 무기를 찾을 수 없습니다."
        _, 스킬명, wa, _, _, _, _, _ = 무기[무기이름]
        스킬lv = c["스킬"].get(스킬명, 0)
        # 전투감각(솔로)은 이니셔티브와 경계에만 붙는다. 명중에는 더하지 않는다.
    else:
        n = NPC(channel_id, 공격자_npc)
        if n is None:
            return None, f"'{공격자_npc}' NPC가 없습니다."
        공격자이름 = n["name"]
        ref = n["ref"] + 부상페널티(n["damage"])
        무기이름 = 무기_별칭.get(무기이름 or n["weapon"] or "중권총", 무기이름 or n["weapon"] or "중권총")
        if 무기이름 not in 무기:
            return None, f"'{무기이름}' 무기를 찾을 수 없습니다."
        _, 스킬명, wa, _, _, _, _, _ = 무기[무기이름]
        스킬lv = n["skill"]

    분류, 스킬명, wa, 피해식, 연사최대, 사거리m, _, _ = 무기[무기이름]

    # ---- 대상 ----
    대상객체, 대상이름, 대상종류 = None, None, None
    if 대상 is not None:
        대상객체 = NPC(channel_id, str(대상))
        if 대상객체 is not None:
            대상이름, 대상종류 = 대상객체["name"], "npc"
        else:
            대상객체 = 캐릭터(대상)
            if 대상객체 is not None:
                대상이름, 대상종류 = 대상객체["name"], "pc"
    if 대상객체 is None:
        return None, f"'{대상}' 대상을 찾을 수 없습니다. NPC는 `!TRPG NPC추가` 로 먼저 등록하세요."

    # ---- 명중 ----
    사거리키 = 사거리_별칭.get(str(사거리 or "").lower(), 사거리)
    if 사거리키 not in 사거리표:
        사거리키 = "중거리"
    목표, _ = 사거리표[사거리키]
    보정 = int(보정 or 0)

    눈, 기록, 대성공, 펌블 = 판정주사위()
    총합 = 눈 + ref + 스킬lv + wa + 보정
    명중 = 총합 >= 목표

    결과 = {
        "공격자": 공격자이름, "대상": 대상이름, "대상종류": 대상종류,
        "무기": 무기이름, "사거리": 사거리키, "목표": 목표,
        "주사위": 눈, "기록": 기록, "REF": ref, "스킬": 스킬명, "스킬레벨": 스킬lv,
        "WA": wa, "보정": 보정, "총합": 총합, "명중": 명중,
        "대성공": 대성공, "펌블": 펌블, "타격": [],
        "식": f"1D10({기록}) + REF {ref} + {스킬명} {스킬lv}" + (f" + WA {wa:+d}" if wa else "") + (f" + 보정 {보정:+d}" if 보정 else ""),
    }
    if not 명중:
        return 결과, None

    # ---- 연사 ----
    try:
        연사 = max(1, int(연사 or 1))
    except (TypeError, ValueError):
        연사 = 1
    연사 = min(연사, max(1, 연사최대))
    if 연사 > 1:
        # 목표치를 넘긴 만큼 추가로 맞는다 (3 초과마다 1발, 최대 연사수)
        추가 = min(연사, 1 + max(0, (총합 - 목표) // 3))
    else:
        추가 = 1
    결과["발수"] = 추가

    총피해 = 0
    for _ in range(추가):
        눈부위, 부위이름, 칸 = 부위판정()
        피해량, 눈들, _ = 굴림(피해식)
        실제, 줄 = 피해계산(대상객체, 피해량, 칸, 부위이름)
        총피해 += 실제
        결과["타격"].append({
            "부위": 부위이름, "부위눈": 눈부위, "굴림": 눈들,
            "피해식": 피해식, "원피해": 피해량, "실피해": 실제, "설명": 줄,
        })

    결과["총피해"] = 총피해
    if 총피해 > 0:
        적용, _ = 피해적용(channel_id, 대상이름 if 대상종류 == "npc" else 대상, 총피해,
                         NPC여부=(대상종류 == "npc"))
        결과["대상상태"] = 적용
    return 결과, None


def 피해적용(channel_id, 대상, 피해, NPC여부=False):
    피해 = max(0, int(피해))
    # 봇과 웹은 서로 다른 프로세스라 동시에 때릴 수 있다.
    # 읽고-더하고-쓰면 한쪽 피해가 사라지므로 DB 안에서 더한다.
    if NPC여부:
        n = NPC(channel_id, str(대상))
        if n is None:
            return None, f"'{대상}' NPC가 없습니다."
        cursor.execute("UPDATE trpg_npcs SET damage = damage + ? WHERE channel_id = ? AND name = ?",
                       (피해, str(channel_id), n["name"]))
        conn.commit()
        cursor.execute("SELECT damage FROM trpg_npcs WHERE channel_id = ? AND name = ?",
                       (str(channel_id), n["name"]))
        행 = cursor.fetchone()
        새피해 = 행[0] if 행 else n["damage"] + 피해
        body, 이름 = n["body"], n["name"]
    else:
        c = 캐릭터(대상)
        if c is None:
            return None, "대상 캐릭터가 없습니다."
        cursor.execute("UPDATE trpg_characters SET damage = damage + ? WHERE user_id = ?", (피해, c["_uid"]))
        conn.commit()
        cursor.execute("SELECT damage FROM trpg_characters WHERE user_id = ?", (c["_uid"],))
        행 = cursor.fetchone()
        새피해 = 행[0] if 행 else c["damage"] + 피해
        body, 이름 = c["stat_body"], c["name"]

    상태, 스턴페널티, 치명 = 부상정보(새피해)
    결과 = {"이름": 이름, "누적피해": 새피해, "상태": 상태, "치명단계": 치명, "입은피해": 피해}

    if 피해 > 0:
        눈 = d10()
        목표 = body + 스턴페널티
        결과["스턴세이브"] = {"주사위": 눈, "목표": 목표, "성공": 눈 <= 목표}
    if 치명 is not None:
        눈 = d10()
        목표 = body - 치명
        결과["데스세이브"] = {"주사위": 눈, "목표": 목표, "성공": 눈 <= 목표}
    return 결과, None


def 회복(대상_uid, 양=None):
    c = 캐릭터(대상_uid)
    if c is None:
        return False, "캐릭터가 없습니다."
    if 양 is None:
        새 = 0
    else:
        새 = max(0, c["damage"] - int(양))
    cursor.execute("UPDATE trpg_characters SET damage = ? WHERE user_id = ?", (새, c["_uid"]))
    conn.commit()
    상태, _, _ = 부상정보(새)
    return True, f"{c['name']} 의 누적 피해가 {c['damage']} → {새} ({상태}) 가 되었습니다."


def 안정화(의사_uid, 환자, NPC여부=False, channel_id=None, 보정=0):
    """TECH + 의료스킬 + 1D10 >= 환자의 누적 피해"""
    d = 캐릭터(의사_uid)
    if d is None:
        return None, "시술자 캐릭터가 없습니다."
    if NPC여부:
        p = NPC(channel_id, str(환자))
        if p is None:
            return None, f"'{환자}' NPC가 없습니다."
        누적, 환자이름 = p["damage"], p["name"]
    else:
        p = 캐릭터(환자)
        if p is None:
            return None, "환자 캐릭터가 없습니다."
        누적, 환자이름 = p["damage"], p["name"]

    스킬lv = max(d["스킬"].get("응급처치", 0), d["스킬"].get("수술", 0))
    눈 = d10()
    총합 = 눈 + 능력(d, "TECH") + 스킬lv + int(보정 or 0)
    return {
        "의사": d["name"], "환자": 환자이름, "주사위": 눈,
        "TECH": 능력(d, "TECH"), "스킬레벨": 스킬lv, "보정": int(보정 or 0),
        "총합": 총합, "목표": 누적, "성공": 총합 >= 누적,
    }, None


# ============================================================
# 장비
# ============================================================

def _돈차감(user_id, 금액):
    """
    돈을 뺀다. 모자라면 False.
    확인과 차감을 한 SQL 로 묶어야 봇과 웹이 동시에 사도 잔고가 음수로 안 내려간다.
    """
    금액 = int(금액)
    if 금액 <= 0:
        return True
    cursor.execute("UPDATE trpg_characters SET money = money - ? WHERE user_id = ? AND money >= ?",
                   (금액, str(user_id), 금액))
    if cursor.rowcount > 0:
        return True
    # 돈이 모자라 아무것도 안 바뀌었어도 UPDATE 가 트랜잭션을 열어둔다.
    # 되돌리지 않으면 이 연결이 쓰기 잠금을 쥔 채로 남아 다른 쪽이 'database is locked' 를 맞는다.
    conn.rollback()
    return False


def _장비보유(user_id, 종류, 이름):
    cursor.execute("SELECT qty FROM trpg_gear WHERE user_id = ? AND kind = ? AND name = ?", (str(user_id), 종류, 이름))
    r = cursor.fetchone()
    return bool(r and r[0] > 0)


def 장비장착(user_id, 이름):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    이름 = 무기_별칭.get(이름, 이름)
    if 이름 in 무기 and 이름 != "주먹" and not _장비보유(c["_uid"], "weapon", 이름):
        return False, f"{이름} 을(를) 가지고 있지 않습니다. `!TRPG 구매 {이름}` 으로 먼저 사세요."
    if 이름 in 방어구 and not _장비보유(c["_uid"], "armor", 이름):
        return False, f"{이름} 을(를) 가지고 있지 않습니다. `!TRPG 구매 {이름}` 으로 먼저 사세요."
    if 이름 in 무기:
        cursor.execute("UPDATE trpg_characters SET weapon = ? WHERE user_id = ?", (이름, c["_uid"]))
        conn.commit()
        return True, f"{이름} 을(를) 들었습니다."
    if 이름 in 방어구:
        cursor.execute("UPDATE trpg_characters SET armor = ? WHERE user_id = ?", (이름, c["_uid"]))
        conn.commit()
        sp, 부위들, _, _ = 방어구[이름]
        return True, f"{이름}(SP {sp}) 을(를) 입었습니다."
    return False, f"'{이름}' 장비를 찾을 수 없습니다."


def 구매(user_id, 이름, 수량=1):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    이름 = 무기_별칭.get(이름, 이름)
    if 이름 in 무기:
        가격, 종류 = 무기[이름][6], "weapon"
    elif 이름 in 방어구:
        가격, 종류 = 방어구[이름][2], "armor"
    elif 이름 in 아이템:
        가격, 종류 = 아이템[이름][0], "item"
    else:
        return False, f"'{이름}' 을(를) 상점에서 찾을 수 없습니다."
    try:
        수량 = max(1, int(수량 or 1))
    except (TypeError, ValueError):
        return False, "수량은 숫자로 입력해주세요."
    총액 = 가격 * 수량
    if not _돈차감(c["_uid"], 총액):
        return False, f"돈이 부족합니다. {총액:,}eb 필요 (보유 {c['money']:,}eb)"
    cursor.execute(
        "INSERT INTO trpg_gear (user_id, kind, name, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, kind, name) DO UPDATE SET qty = qty + ?",
        (c["_uid"], 종류, 이름, 수량, 수량))
    conn.commit()
    return True, f"{이름} ×{수량} 구매 ({총액:,}eb). 남은 돈 {캐릭터(user_id)['money']:,}eb"


def 소지품(user_id):
    cursor.execute("SELECT kind, name, qty FROM trpg_gear WHERE user_id = ? ORDER BY kind, name", (str(user_id),))
    return cursor.fetchall()


# ============================================================
# 라이프패스
# ============================================================

출신표 = ["북미", "남미", "서유럽", "동유럽", "중동", "아프리카", "일본", "중국·동아시아", "동남아", "태평양 연안"]
민족언어 = {
    "북미": "영어", "남미": "스페인어", "서유럽": "독일어·프랑스어", "동유럽": "러시아어",
    "중동": "아랍어", "아프리카": "스와힐리어", "일본": "일본어",
    "중국·동아시아": "중국어·한국어", "동남아": "말레이어", "태평양 연안": "영어·타갈로그어",
}
복장표 = ["너덜너덜한 거리 패션", "광택 나는 가죽", "기업 정장", "유행 지난 레트로", "군복 스타일",
          "원색의 네오키치", "검은 옷 일색", "노출이 많은 하이패션", "작업복과 공구 벨트", "미니멀한 무채색"]
머리표 = ["짧게 민 머리", "길고 거친 머리", "화려한 염색", "모히칸", "완전 삭발",
          "땋아 올린 머리", "젤로 세운 머리", "레이저로 새긴 문양", "흐트러진 장발", "단정한 단발"]
특징표 = ["문신", "거울 같은 의안", "피어싱", "얼굴 흉터", "기묘한 치아", "짙은 화장",
          "금속 손톱", "목에 박힌 플러그", "빛나는 피부 회로", "향수 냄새가 진함"]

가족배경표 = ["기업 임원", "기업 관리직", "기업 기술직", "노매드 무리", "해적 선단",
              "갱 패밀리", "범죄 조직 보스", "전투 구역 빈민", "도시 노숙자", "아콜로지 주민"]
부모표 = [
    (6, "부모가 둘 다 살아 있다", None),
    (10, "부모에게 무슨 일이 있었다", "부모사건"),
]
부모사건표 = ["전쟁으로 돌아가셨다", "사고로 돌아가셨다", "살해당했다", "기억을 잃고 너를 못 알아본다",
              "부모를 한 번도 본 적이 없다", "너를 지키려고 숨어 있다", "친척에게 맡겨졌다",
              "거리에서 자랐고 부모가 없었다", "너를 입양 보냈다", "너를 돈 받고 팔았다"]
가족사건표 = ["가족이 적에게 몰살당했다", "가족이 누명을 쓰고 몰락했다", "가족의 재산을 전부 잃었다",
              "가족이 뿔뿔이 흩어졌다", "가족이 빚 때문에 쫓기고 있다", "가족 중 하나가 배신했다",
              "가족이 기업에 통째로 팔렸다", "가족이 전쟁 통에 실종됐다",
              "가족이 마약에 손댔다 망했다", "가족의 원수가 아직 살아 있다"]
유년환경표 = ["어른 없이 거리에서", "안전한 기업 교외 주택가에서", "떠돌아다니는 노매드 무리에서",
              "한때 잘나갔던 낡은 동네에서", "도심 기업 보호구역에서", "전투 구역 한복판에서",
              "도시에서 먼 작은 마을에서", "거대한 농업 단지에서", "우주 정거장이나 궤도 거주구에서",
              "교도소나 수용 시설에서"]

성격표 = ["수줍고 속을 안 보인다", "반항적이고 폭력적이다", "오만하고 거리를 둔다",
          "기분파에 성급하다", "까다롭고 예민하다", "차분하고 진지하다",
          "실없고 가볍다", "교활하고 남을 잘 속인다", "이지적이고 초연하다", "붙임성 있고 활달하다"]
소중한사람표 = ["부모", "형제자매", "연인", "친구", "자기 자신", "반려동물",
                "스승", "유명인", "개인적인 영웅", "아무도 없다"]
가치관표 = ["돈", "명예", "한번 뱉은 말", "정직함", "지식", "복수", "사랑", "힘", "즐거움", "우정"]
타인관표 = ["그냥 그렇다", "그냥 그렇다", "거의 모두를 좋아한다", "거의 모두를 싫어한다",
            "사람은 도구다. 쓰고 버린다", "한 사람 한 사람이 다 소중하다",
            "앞을 가로막으면 치워버린다", "아무도 믿지 않는다",
            "다 쓸어버리고 바퀴벌레한테 넘겨주고 싶다", "사람은 멋진 존재다"]
소중한물건표 = ["무기", "공구", "옷 한 벌", "사진 한 장", "책이나 일기", "녹음 기록",
                "악기", "장신구", "낡은 장난감", "편지 한 통"]

인생사건표 = [
    (1, 2, "큰일 없이 지나간 해"),
    (3, 3, "운이 트이거나 바닥을 쳤다"),
    (4, 4, "친구나 적이 생겼다"),
    (5, 5, "연애를 했다"),
    (6, 6, "돈이 걸린 일에 휘말렸다"),
    (7, 7, "거리에서 이름이 알려지기 시작했다"),
    (8, 8, "누군가에게 빚을 졌다"),
    (9, 9, "몸에 금속을 심었다"),
    (10, 10, "죽을 뻔했다"),
]
대박표 = ["큰돈을 만졌다 (1D10×100eb)", "쓸 만한 장비를 손에 넣었다", "유력한 후원자가 생겼다",
          "기업 하나를 엿먹였다", "거리에서 평판이 올랐다", "비싼 사이버웨어를 공짜로 얻었다",
          "빚을 전부 갚았다", "적 하나가 사라졌다", "쓸모 있는 정보를 쥐었다", "목숨을 하나 건졌다"]
쪽박표 = ["빚을 졌다 (1D10×100eb)", "감옥에 갇혔다 (1D10개월)", "병을 얻거나 약에 손댔다",
          "장비를 전부 잃었다", "거리에서 평판이 떨어졌다", "몸 일부를 잃었다",
          "믿던 사람에게 배신당했다", "기업의 표적이 됐다", "친구가 죽었다", "쫓기는 신세가 됐다"]
적생김표 = ["체면을 구겼다", "연인·친구·가족을 잃게 만들었다", "크게 망신을 줬다",
            "돈 문제로 틀어졌다", "일을 망쳤다", "애인을 가로챘다",
            "비밀을 폭로했다", "조직을 배신했다", "가족을 건드렸다", "그냥 서로 맞지 않았다"]
연애표 = ["행복하게 이어지는 중", "비극으로 끝났다", "상대가 떠나버렸다", "상대가 죽었다",
          "서로 원수가 됐다", "아직 짝사랑 중", "들키면 안 되는 사이", "가족이 반대해서 깨졌다",
          "일 때문에 멀어졌다", "상대가 사라졌고 아직 찾는 중"]


def 라이프패스생성(나이=None):
    """캐릭터의 과거를 통째로 굴린다. dict 반환"""
    def 뽑(표):
        return random.choice(표)

    출신 = 뽑(출신표)
    결과 = {
        "출신": 출신,
        "언어": 민족언어.get(출신, "영어"),
        "복장": 뽑(복장표),
        "머리": 뽑(머리표),
        "특징": 뽑(특징표),
        "가족배경": 뽑(가족배경표),
        "유년환경": 뽑(유년환경표),
        "성격": 뽑(성격표),
        "소중한사람": 뽑(소중한사람표),
        "가치관": 뽑(가치관표),
        "타인관": 뽑(타인관표),
        "소중한물건": 뽑(소중한물건표),
    }

    눈 = d10()
    if 눈 <= 6:
        결과["부모"] = "부모가 둘 다 살아 있다"
    else:
        결과["부모"] = 뽑(부모사건표)
    결과["가족사건"] = 뽑(가족사건표) if d10() <= 6 else "가족은 그럭저럭 버티고 있다"

    if 나이 is None:
        나이 = 16 + random.randint(1, 6) + random.randint(1, 6)
    나이 = max(16, min(60, int(나이)))
    결과["나이"] = 나이

    사건들 = []
    for 해 in range(17, 나이 + 1):
        눈 = d10()
        종류 = next((t for lo, hi, t in 인생사건표 if lo <= 눈 <= hi), "큰일 없이 지나간 해")
        상세 = 종류
        if "운이 트이" in 종류:
            상세 = (뽑(대박표) if d10() % 2 == 0 else 뽑(쪽박표))
        elif "친구나 적" in 종류:
            상세 = ("친구가 생겼다" if d10() <= 5 else f"적이 생겼다 — {뽑(적생김표)}")
        elif "연애" in 종류:
            상세 = f"연애 — {뽑(연애표)}"
        elif "몸에 금속" in 종류:
            상세 = "사이버웨어를 하나 심었다"
        사건들.append({"나이": 해, "내용": 상세})
    결과["사건"] = 사건들
    return 결과


def 라이프패스저장(user_id, 데이터):
    cursor.execute("UPDATE trpg_characters SET lifepath = ? WHERE user_id = ?",
                   (json.dumps(데이터, ensure_ascii=False), str(user_id)))
    conn.commit()


def 라이프패스읽기(user_id):
    cursor.execute("SELECT lifepath FROM trpg_characters WHERE user_id = ?", (str(user_id),))
    r = cursor.fetchone()
    if not r or not r[0]:
        return None
    try:
        return json.loads(r[0])
    except ValueError:
        return None


def 라이프패스글(d):
    if not d:
        return "아직 과거를 굴리지 않았습니다."
    줄 = [
        f"**출신** {d['출신']} · {d['언어']} 사용 · {d['나이']}세",
        f"**겉모습** {d['복장']}, {d['머리']}, {d['특징']}",
        f"**집안** {d['가족배경']} 출신. {d['부모']}. {d['가족사건']}",
        f"**자란 곳** {d['유년환경']}",
        f"**성격** {d['성격']}. 사람에 대해선 — {d['타인관']}",
        f"**소중한 것** {d['소중한사람']} · {d['가치관']} · {d['소중한물건']}",
    ]
    if d.get("사건"):
        줄.append("**지나온 해**")
        for e in d["사건"]:
            줄.append(f"  {e['나이']}세 — {e['내용']}")
    return "\n".join(줄)


# ============================================================
# 사이버웨어와 인간성
# ============================================================
# 이름: (분류, 인간성손실 주사위, 가격eb, 효과설명, 보너스dict)

사이버웨어 = {
    # --- 신경계 ---
    "뉴럴프로세서":   ("신경", "2D6", 1000, "다른 신경계 사이버웨어를 달기 위한 기반", {}),
    "인터페이스플러그": ("신경", "1D6", 200, "넷러닝과 차량 직결에 필요", {}),
    "반사부스터1":    ("신경", "2D6", 5000,  "REF +1", {"REF": 1}),
    "반사부스터2":    ("신경", "2D6", 10000, "REF +2", {"REF": 2}),
    "반사부스터3":    ("신경", "3D6", 20000, "REF +3", {"REF": 3}),
    "산데비스탄":     ("신경", "3D6", 15000, "턴당 한 번 이니셔티브 +3", {"이니셔티브": 3}),
    "통각차단기":     ("신경", "2D6", 2000,  "스턴 세이브 +3", {"스턴": 3}),
    "스킬칩소켓":     ("신경", "1D6", 1000,  "칩을 꽂아 스킬을 즉시 얻는다", {}),
    # --- 감각 ---
    "사이버옵틱":     ("감각", "2D6", 1000, "의안. 아래 옵션을 끼울 수 있다", {}),
    "타겟스코프":     ("감각", "1D6", 600,  "조준한 사격에 명중 +1", {"명중": 1}),
    "저조도시야":     ("감각", "1D6", 500,  "어둠 속 페널티 무시", {}),
    "열화상":         ("감각", "1D6", 500,  "열로 본다. 연막·어둠 무시", {}),
    "망원렌즈":       ("감각", "1D6", 400,  "원거리·극원거리 명중 +2", {}),
    "사이버오디오":   ("감각", "2D6", 1000, "의이. 아래 옵션을 끼울 수 있다", {}),
    "증폭청각":       ("감각", "1D6", 500,  "경계 판정 +2", {"경계": 2}),
    "레벨댐퍼":       ("감각", "1D6", 300,  "섬광·폭음에 영향받지 않는다", {}),
    # --- 신체 ---
    "사이버팔":       ("신체", "2D6", 3000, "맨손 피해 2D6, 들어올리는 힘 2배", {"맨손피해": "2D6"}),
    "사이버다리":     ("신체", "2D6", 3000, "MA +2, 낙하 피해 절반", {"MA": 2}),
    "강화골격":       ("신체", "3D6", 5000, "BODY +2", {"BODY": 2}),
    "피부직조":       ("신체", "2D6", 3000, "전신 SP +6", {"SP": 6}),
    "서브더멀아머":   ("신체", "3D6", 5000, "전신 SP +12, 눈에 띈다", {"SP": 12}),
    "근육이식":       ("신체", "2D6", 4000, "BODY +1, 격투 피해 +2", {"BODY": 1}),
    # --- 내장 무기 ---
    "손톱날":         ("무기", "1D6", 1000, "숨겨진 날. 피해 1D6+3", {}),
    "팝업권총":       ("무기", "2D6", 2000, "팔에 숨긴 권총. 피해 2D6+1", {}),
    "모노와이어":     ("무기", "3D6", 7000, "단분자 철사. 피해 3D6, 방어구 무시", {}),
    # --- 생체 ---
    "생체모니터":     ("생체", "1D6", 500,  "몸 상태를 실시간으로 본다", {}),
    "독소정화기":     ("생체", "2D6", 2000, "독·약물 저항 +4", {}),
    "나노수복기":     ("생체", "2D6", 5000, "하루에 피해 2씩 자동 회복", {}),
}

사이버웨어_별칭 = {
    "speedware": "반사부스터1", "sandevistan": "산데비스탄",
    "cyberarm": "사이버팔", "cybereye": "사이버옵틱", "cyberoptic": "사이버옵틱",
    "skinweave": "피부직조", "의안": "사이버옵틱", "의수": "사이버팔", "의족": "사이버다리",
}

인간성_경고 = 20          # 남은 인간성이 이 아래면 경고
사이버사이코_선 = 0        # 0 이하가 되면 사이버사이코


def 설치목록(user_id):
    cursor.execute("SELECT name, qty FROM trpg_gear WHERE user_id = ? AND kind = 'cyber'", (str(user_id),))
    return {n: q for n, q in cursor.fetchall()}


def 사이버보너스(user_id):
    """설치한 사이버웨어의 보너스를 합산"""
    합 = {"REF": 0, "BODY": 0, "MA": 0, "SP": 0, "이니셔티브": 0, "스턴": 0, "명중": 0, "경계": 0}
    for 이름, 개수 in 설치목록(user_id).items():
        d = 사이버웨어.get(이름)
        if not d:
            continue
        for k, v in d[4].items():
            if k not in 합 or not isinstance(v, int):
                continue
            if k == "SP":
                합[k] = max(합[k], v)        # 피하 장갑은 겹쳐도 가장 두꺼운 것만
            else:
                합[k] += v * 개수
    return 합


def 사이버설치(user_id, 이름, 결제=True):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    이름 = 사이버웨어_별칭.get(str(이름).lower(), 이름)
    if 이름 not in 사이버웨어:
        return False, f"'{이름}' 사이버웨어를 찾을 수 없습니다. `!TRPG 사이버목록` 으로 확인하세요."
    분류, 손실식, 가격, 설명, 보너스 = 사이버웨어[이름]


    손실, 눈들, _ = 굴림(손실식)
    새손실 = c["humanity_lost"] + 손실
    기준EMP = EMP기준(c)
    최대 = 기준EMP * 10
    남은 = max(0, 최대 - 새손실)

    # 인간성 10 떨어질 때마다 EMP 1 감소 (원래 EMP 기준)
    새EMP = max(0, 기준EMP - 새손실 // 10)

    if 결제 and not _돈차감(c["_uid"], 가격):
        return False, f"돈이 부족합니다. {가격:,}eb 필요 (보유 {c['money']:,}eb)"
    cursor.execute("UPDATE trpg_characters SET humanity_lost = humanity_lost + ?, stat_emp = ? WHERE user_id = ?",
                   (손실, 새EMP, c["_uid"]))
    cursor.execute(
        "INSERT INTO trpg_gear (user_id, kind, name, qty) VALUES (?, 'cyber', ?, 1) "
        "ON CONFLICT(user_id, kind, name) DO UPDATE SET qty = qty + 1", (c["_uid"], 이름))
    conn.commit()

    줄 = [f"🔩 **{이름}** 설치 완료 ({분류})",
          f"{설명}",
          f"인간성 손실 {손실식}({'+'.join(map(str, 눈들))}) = **{손실}**",
          f"남은 인간성 **{남은}** / {최대}"]
    if 새EMP < c["stat_emp"]:
        줄.append(f"⚠️ EMP 가 {c['stat_emp']} → {새EMP} 로 떨어졌다")
    if 남은 <= 사이버사이코_선:
        줄.append("💀 **사이버사이코.** 더 이상 사람으로 볼 수 없다. GM이 캐릭터를 회수한다")
    elif 남은 <= 인간성_경고:
        줄.append("⚠️ 인간성이 바닥나고 있다. 사람 얼굴이 고깃덩이로 보이기 시작한다")
    return True, "\n".join(줄)


def 사이버제거(user_id, 이름):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    이름 = 사이버웨어_별칭.get(str(이름).lower(), 이름)
    가진 = 설치목록(user_id)
    if 이름 not in 가진:
        return False, f"'{이름}' 을(를) 달고 있지 않습니다."
    if 가진[이름] <= 1:
        cursor.execute("DELETE FROM trpg_gear WHERE user_id = ? AND kind = 'cyber' AND name = ?", (c["_uid"], 이름))
    else:
        cursor.execute("UPDATE trpg_gear SET qty = qty - 1 WHERE user_id = ? AND kind = 'cyber' AND name = ?",
                       (c["_uid"], 이름))
    conn.commit()
    return True, f"{이름} 을(를) 떼어냈습니다. 잃어버린 인간성은 돌아오지 않습니다."


def 사이버사이코판정(user_id, 보정=0):
    """인간성이 낮을 때 COOL 로 자제력을 시험한다"""
    c = 캐릭터(user_id)
    if c is None:
        return None, "캐릭터가 없습니다."
    남은 = 인간성(c)
    목표 = 15 + max(0, (30 - 남은) // 5)
    눈, 기록, 대성공, 펌블 = 판정주사위()
    총합 = 눈 + 능력(c, "COOL") + c["스킬"].get("의지", 0) + int(보정 or 0)
    return {
        "이름": c["name"], "남은인간성": 남은, "주사위": 눈, "기록": 기록,
        "COOL": 능력(c, "COOL"), "총합": 총합, "목표": 목표, "성공": 총합 >= 목표,
    }, None


# ============================================================
# 네트러닝
# ============================================================
# 프로그램: (분류, 강도STR, 가격eb, 설명)

프로그램 = {
    # 침입
    "해머":       ("침입", 4, 1000, "ICE 를 정면으로 부순다. 피해 1D10"),
    "잭해머":     ("침입", 6, 2000, "강력한 ICE 파괴. 피해 2D6+3"),
    "소드":       ("침입", 5, 1500, "정밀하게 ICE 를 벤다. 피해 1D6+3"),
    "웜":         ("침입", 3, 800,  "몰래 파고든다. 탐지되지 않음"),
    # 방어
    "실드":       ("방어", 3, 600,  "받는 피해 -3"),
    "아머":       ("방어", 5, 1200, "받는 피해 -5"),
    "플랫라인방지": ("방어", 4, 2500, "블랙 ICE 치명타를 1회 무효화"),
    # 탐지
    "씨야리얼":   ("탐지", 4, 700,  "숨은 ICE 와 파일을 드러낸다"),
    "스피드트랩": ("탐지", 3, 500,  "함정과 경보를 미리 읽는다"),
    # 유틸
    "리플레이":   ("유틸", 3, 600,  "잠금을 풀고 파일을 연다"),
    "인비저빌리티": ("유틸", 4, 1500, "시스템이 나를 못 본다"),
    "허수아비":   ("유틸", 5, 1800, "가짜 신호로 ICE 를 속인다"),
}

# ICE: (분류, 강도STR, 피해식, 설명)
ICE = {
    "워치독":     ("경보", 3, None,    "침입을 발견하면 경보를 울린다"),
    "블러드하운드": ("추적", 4, None,  "침입자의 실제 위치를 역추적한다"),
    "라이엇":     ("공격", 5, "1D6",   "덱을 공격해 접속을 끊으려 한다"),
    "킬러":       ("공격", 6, "2D6",   "프로그램을 찢어버린다"),
    "헬하운드":   ("블랙", 7, "2D6+2", "넷러너의 뇌를 직접 태운다"),
    "리퍼":       ("블랙", 8, "3D6",   "접속한 사람을 죽이려고 만들어졌다"),
    "월":         ("방벽", 5, None,    "그냥 두꺼운 벽. 부수고 지나가야 한다"),
    "리버서":     ("방벽", 6, None,    "명령을 거꾸로 되돌린다"),
}

노드종류 = ["CPU", "메모리", "데이터월", "파일서버", "SPU", "게이트웨이", "터미널"]


def 넷시스템생성(channel_id, 이름, 난이도="보통", 층수=None):
    """침입할 시스템을 만든다. 노드마다 ICE 가 붙는다"""
    등급 = {"쉬움": 1, "보통": 2, "어려움": 3, "매우어려움": 4}.get(str(난이도), 2)
    층수 = int(층수 or (2 + 등급))
    약한ICE = [k for k, v in ICE.items() if v[1] <= 5]
    강한ICE = [k for k, v in ICE.items() if v[1] >= 6]

    노드들 = []
    for i in range(층수):
        마지막 = (i == 층수 - 1)
        종류 = "CPU" if 마지막 else random.choice(노드종류[1:])
        경비 = []
        수 = 1 if i == 0 else min(2, 1 + (등급 // 2))
        for _ in range(수):
            경비.append(random.choice(강한ICE if (마지막 or 등급 >= 3) else 약한ICE))
        노드들.append({"번호": i + 1, "종류": 종류, "ICE": 경비, "뚫림": False})

    데이터 = {"이름": 이름, "난이도": 난이도, "등급": 등급, "노드": 노드들, "경보": 0}
    cursor.execute("INSERT OR REPLACE INTO trpg_net (channel_id, name, data) VALUES (?, ?, ?)",
                   (str(channel_id), 이름, json.dumps(데이터, ensure_ascii=False)))
    conn.commit()
    return 데이터


def 넷시스템(channel_id, 이름):
    cursor.execute("SELECT data FROM trpg_net WHERE channel_id = ? AND name = ?", (str(channel_id), 이름))
    r = cursor.fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except ValueError:
        return None


def 넷시스템저장(channel_id, 데이터):
    cursor.execute("UPDATE trpg_net SET data = ? WHERE channel_id = ? AND name = ?",
                   (json.dumps(데이터, ensure_ascii=False), str(channel_id), 데이터["이름"]))
    conn.commit()


def 넷시스템목록(channel_id):
    cursor.execute("SELECT name FROM trpg_net WHERE channel_id = ?", (str(channel_id),))
    return [r[0] for r in cursor.fetchall()]


def 넷시스템삭제(channel_id, 이름=None):
    if 이름:
        cursor.execute("DELETE FROM trpg_net WHERE channel_id = ? AND name = ?", (str(channel_id), 이름))
    else:
        cursor.execute("DELETE FROM trpg_net WHERE channel_id = ?", (str(channel_id),))
    conn.commit()
    return "시스템을 지웠습니다."


def 넷침입(user_id, channel_id, 시스템이름, 프로그램이름=None, 보정=0):
    """
    노드 하나를 뚫는다. 인터페이스 판정으로 ICE 와 맞선다.
    1D10 + INT + 인터페이스 + 프로그램 강도 vs ICE 강도 + 10
    """
    c = 캐릭터(user_id)
    if c is None:
        return None, "캐릭터가 없습니다."
    sys = 넷시스템(channel_id, 시스템이름)
    if sys is None:
        return None, f"'{시스템이름}' 시스템이 없습니다. GM이 먼저 만들어야 합니다."

    남은노드 = [n for n in sys["노드"] if not n["뚫림"]]
    if not 남은노드:
        return None, f"{시스템이름} 은(는) 이미 전부 뚫렸습니다."
    노드 = 남은노드[0]

    인터페이스 = c["스킬"].get("인터페이스", 0)
    if c["role"] == "넷러너":
        인터페이스 = max(인터페이스, c["special"])

    프로그램강도 = 0
    if 프로그램이름:
        if 프로그램이름 not in 프로그램:
            return None, f"'{프로그램이름}' 프로그램을 찾을 수 없습니다."
        if not 보유프로그램(user_id, 프로그램이름):
            return None, f"{프로그램이름} 을(를) 가지고 있지 않습니다. `!TRPG 넷상점` 에서 사세요."
        프로그램강도 = 프로그램[프로그램이름][1]

    현재ICE = 노드["ICE"][0] if 노드["ICE"] else None
    ice강도 = ICE[현재ICE][1] if 현재ICE else 0

    눈, 기록, 대성공, 펌블 = 판정주사위()
    if 현재ICE:
        # ICE 와의 싸움은 프로그램끼리 붙는 것이다. 맨손으로는 거의 이길 수 없다.
        총합 = 눈 + 프로그램강도 + 인터페이스 // 2 + int(보정 or 0)
        ice눈 = d10()
        목표 = ice눈 + ice강도 + sys["등급"]
        식종류 = "program"
    else:
        # 경비가 없는 노드는 잠금만 풀면 된다
        총합 = 눈 + 능력(c, "INT") + 인터페이스 + int(보정 or 0)
        ice눈 = 0
        목표 = 10 + sys["등급"]
        식종류 = "lock"
    성공 = 총합 >= 목표

    결과 = {
        "이름": c["name"], "시스템": 시스템이름, "노드": 노드["번호"], "노드종류": 노드["종류"],
        "ICE": 현재ICE, "ICE강도": ice강도, "프로그램": 프로그램이름, "프로그램강도": 프로그램강도,
        "주사위": 눈, "기록": 기록, "INT": 능력(c, "INT"), "인터페이스": 인터페이스,
        "ICE주사위": ice눈, "등급": sys["등급"], "식종류": 식종류,
        "총합": 총합, "목표": 목표, "성공": 성공, "대성공": 대성공, "펌블": 펌블,
        "반격": None, "돌파": False, "완료": False,
    }

    if 성공:
        if 현재ICE:
            노드["ICE"].pop(0)
            결과["격파"] = 현재ICE
        if not 노드["ICE"]:
            노드["뚫림"] = True
            결과["돌파"] = True
            if all(n["뚫림"] for n in sys["노드"]):
                결과["완료"] = True
    else:
        sys["경보"] += 1
        결과["경보"] = sys["경보"]
        # ICE 가 반격한다
        if 현재ICE:
            분류, 강도, 피해식, _ = ICE[현재ICE]
            if 피해식:
                피해량, 눈들, _ = 굴림(피해식)
                감소 = 0
                for p in ("아머", "실드"):
                    if 보유프로그램(user_id, p):
                        감소 = max(감소, {"아머": 5, "실드": 3}[p])
                실제 = max(0, 피해량 - 감소)
                결과["반격"] = {"ICE": 현재ICE, "분류": 분류, "굴림": 눈들,
                               "원피해": 피해량, "방어": 감소, "피해": 실제}
                if 분류 == "블랙" and 실제 > 0:
                    적용, _ = 피해적용(channel_id, user_id, 실제, NPC여부=False)
                    결과["반격"]["뇌피해"] = 적용

    넷시스템저장(channel_id, sys)
    return 결과, None


def 보유프로그램(user_id, 이름):
    cursor.execute("SELECT qty FROM trpg_gear WHERE user_id = ? AND kind = 'program' AND name = ?",
                   (str(user_id), 이름))
    r = cursor.fetchone()
    return bool(r and r[0] > 0)


def 프로그램구매(user_id, 이름, 수량=1):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    if 이름 not in 프로그램:
        return False, f"'{이름}' 프로그램이 없습니다."
    분류, 강도, 가격, 설명 = 프로그램[이름]
    수량 = max(1, int(수량 or 1))
    총액 = 가격 * 수량
    if not _돈차감(c["_uid"], 총액):
        return False, f"돈이 부족합니다. {총액:,}eb 필요 (보유 {c['money']:,}eb)"
    cursor.execute(
        "INSERT INTO trpg_gear (user_id, kind, name, qty) VALUES (?, 'program', ?, ?) "
        "ON CONFLICT(user_id, kind, name) DO UPDATE SET qty = qty + ?", (c["_uid"], 이름, 수량, 수량))
    conn.commit()
    return True, f"{이름} (강도 {강도}) 구매. {총액:,}eb 지불"


def 넷침입글(r):
    줄 = [f"💻 **{r['이름']}** → {r['시스템']} 노드 {r['노드']} ({r['노드종류']})"]
    if r["ICE"]:
        줄.append(f"🧊 **{r['ICE']}** (강도 {r['ICE강도']}) 가 길을 막는다")
    if r["식종류"] == "program":
        식 = f"1D10({r['기록']}) + {r['프로그램'] or '맨손'} {r['프로그램강도']} + 인터페이스 {r['인터페이스'] // 2}"
        상대 = f"ICE 1D10({r['ICE주사위']}) + 강도 {r['ICE강도']} + 등급 {r['등급']} = {r['목표']}"
    else:
        식 = f"1D10({r['기록']}) + INT {r['INT']} + 인터페이스 {r['인터페이스']}"
        상대 = f"잠금 {r['목표']}"
    줄.append(f"{식} = **{r['총합']}**  vs  {상대}")
    if r["식종류"] == "program" and not r["프로그램"]:
        줄.append("⚠️ 프로그램 없이 맨손으로 ICE 에 달려들고 있다")
    if r["성공"]:
        if r.get("격파"):
            줄.append(f"✅ **{r['격파']} 격파**")
        if r["돌파"]:
            줄.append(f"🚪 노드 {r['노드']} 돌파")
        if r["완료"]:
            줄.append(f"🏁 **{r['시스템']} 장악 완료.** CPU 를 손에 넣었다")
    else:
        줄.append(f"❌ 뚫지 못했다 (경보 {r.get('경보', 0)}회)")
        반격 = r.get("반격")
        if 반격:
            줄.append(f"⚡ **{반격['ICE']}** 반격 — 피해 {반격['원피해']} − 방어 {반격['방어']} = **{반격['피해']}**")
            뇌 = 반격.get("뇌피해")
            if 뇌:
                줄.append(f"🧠 **블랙 ICE 가 뇌를 태운다!** 누적 {뇌['누적피해']} — {뇌['상태']}")
                d = 뇌.get("데스세이브")
                if d:
                    줄.append(f"💀 데스 세이브 {d['주사위']} vs {d['목표']} → " + ("살았다" if d["성공"] else "**뇌사**"))
    return "\n".join(줄)


# ============================================================
# 차량
# ============================================================
# 이름: (분류, SDP, SP, 최고속도kph, 정원, 가격eb, 설명)

차량 = {
    "오토바이":     ("이륜", 20,  5,  200, 2,  3000,  "빠르고 숨기 쉽다. 몸이 그대로 노출된다"),
    "스쿠터":       ("이륜", 15,  3,  100, 2,  800,   "싸구려 발"),
    "소형차":       ("승용", 40,  10, 160, 4,  12000, "흔한 시내용"),
    "세단":         ("승용", 50,  15, 180, 5,  22000, "무난한 중형차"),
    "스포츠카":     ("승용", 45,  12, 280, 2,  60000, "빠르다. 눈에 띈다"),
    "장갑리무진":   ("승용", 80,  30, 180, 6,  150000, "기업 임원용. 총알이 안 박힌다"),
    "밴":           ("화물", 60,  15, 140, 8,  25000, "팀 전체가 탄다"),
    "픽업트럭":     ("화물", 65,  12, 150, 3,  18000, "노매드가 좋아한다"),
    "대형트럭":     ("화물", 120, 20, 120, 3,  80000, "길을 막고 있으면 답이 없다"),
    "장갑차":       ("군용", 150, 40, 110, 10, 250000, "작은 전쟁 하나는 버틴다"),
    "AV-4":         ("비행", 100, 25, 400, 8,  400000, "수직 이착륙기. 하늘로 도망칠 수 있다"),
    "헬기":         ("비행", 70,  15, 300, 6,  200000, "구식이지만 쓸 만하다"),
}

운전난이도 = {
    "평범한 주행": 10, "빗길·야간": 15, "추격전": 20, "곡예 주행": 25, "불가능한 묘기": 30,
}


def 차량구매(user_id, 이름, 수량=1):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    if 이름 not in 차량:
        return False, f"'{이름}' 차량이 없습니다."
    가격 = 차량[이름][5]
    수량 = max(1, int(수량 or 1))
    총액 = 가격 * 수량
    if not _돈차감(c["_uid"], 총액):
        return False, f"돈이 부족합니다. {총액:,}eb 필요 (보유 {c['money']:,}eb)"
    cursor.execute(
        "INSERT INTO trpg_gear (user_id, kind, name, qty) VALUES (?, 'vehicle', ?, ?) "
        "ON CONFLICT(user_id, kind, name) DO UPDATE SET qty = qty + ?", (c["_uid"], 이름, 수량, 수량))
    conn.commit()
    return True, f"{이름} 구매 ({총액:,}eb). 남은 돈 {캐릭터(user_id)['money']:,}eb"


def 운전판정(user_id, 상황="추격전", 보정=0, 차량이름=None):
    c = 캐릭터(user_id)
    if c is None:
        return None, "캐릭터가 없습니다."
    목표 = 운전난이도.get(상황)
    if 목표 is None:
        목표, _ = 난이도값(상황)
    눈, 기록, 대성공, 펌블 = 판정주사위()
    스킬lv = c["스킬"].get("운전", 0)
    총합 = 눈 + 능력(c, "REF") + 스킬lv + int(보정 or 0)
    r = {
        "이름": c["name"], "상황": 상황, "차량": 차량이름, "주사위": 눈, "기록": 기록,
        "REF": 능력(c, "REF"), "스킬레벨": 스킬lv, "총합": 총합, "목표": 목표,
        "성공": 총합 >= 목표, "대성공": 대성공, "펌블": 펌블, "충돌": None,
    }
    if not r["성공"] and (펌블 or 총합 <= 목표 - 5):
        차 = 차량.get(차량이름 or "", None)
        속도 = 차[3] if 차 else 100
        피해식 = f"{max(1, 속도 // 30)}D6"
        피해량, 눈들, _ = 굴림(피해식)
        r["충돌"] = {"피해식": 피해식, "굴림": 눈들, "피해": 피해량}
    return r, None


def 차량피해(channel_id, 차량이름, 피해):
    """차량 SDP 를 깎는다. 0 이하면 폐차"""
    차 = 차량.get(차량이름)
    if 차 is None:
        return None, f"'{차량이름}' 차량이 없습니다."
    분류, sdp, sp, 속도, 정원, 가격, 설명 = 차
    실제 = max(0, int(피해) - sp)
    return {"차량": 차량이름, "SDP": sdp, "SP": sp, "원피해": int(피해), "실피해": 실제,
            "남은SDP": max(0, sdp - 실제), "폐차": 실제 >= sdp}, None


# ============================================================
# 마셜아츠
# ============================================================
# 유파: (난이도, {기술: 보너스}, 설명)

마셜아츠 = {
    "가라테":   (2, {"타격": 2, "발차기": 2, "막기": 2}, "정권과 발차기를 곧게 뻗는다"),
    "유도":     (1, {"막기": 1, "던지기": 3, "잡기": 2, "탈출": 2, "조르기": 2, "그래플": 2}, "상대 힘을 되돌려준다"),
    "복싱":     (1, {"타격": 3, "막기": 3, "회피": 1}, "주먹 하나로 끝낸다"),
    "무에타이": (4, {"타격": 3, "발차기": 3, "막기": 2}, "팔꿈치와 무릎까지 전부 무기다"),
    "최리풋":   (3, {"타격": 2, "발차기": 2, "막기": 2, "회피": 1, "던지기": 1}, "남방 중국권의 유려한 연격"),
    "합기도":   (3, {"막기": 4, "회피": 3, "던지기": 3, "잡기": 1, "탈출": 3}, "맞서지 않고 흘려보낸다"),
    "동물권":   (3, {"타격": 2, "발차기": 2, "막기": 2, "회피": 2}, "짐승의 움직임을 흉내 낸다"),
    "태권도":   (4, {"타격": 3, "발차기": 3, "막기": 2, "회피": 1}, "발차기의 높이와 속도가 다르다"),
    "사바테":   (2, {"발차기": 4, "막기": 1, "회피": 1}, "구두를 신은 채 차는 프랑스 격투술"),
    "레슬링":   (1, {"던지기": 3, "잡기": 4, "탈출": 4, "조르기": 2, "넘어뜨리기": 2, "그래플": 4}, "붙잡으면 끝이다"),
    "카포에라": (3, {"타격": 1, "발차기": 2, "막기": 2, "회피": 2, "넘어뜨리기": 3}, "춤처럼 움직이며 찬다"),
}

격투기술 = {
    "타격":       ("1D6",   "주먹으로 친다. 가장 기본이고 사이버팔이 있으면 2D6 으로 뛴다"),
    "발차기":     ("1D6+2", "발로 찬다. 주먹보다 아프지만 맞히기는 조금 어렵다"),
    "막기":       (None,    "날아오는 공격을 받아낸다. 성공하면 피해를 안 입는다"),
    "회피":       (None,    "몸을 틀어 피한다. 막기와 달리 반격 자세가 무너지지 않는다"),
    "던지기":     ("1D6",   "넘겨버린다. 상대는 쓰러져서 다음 턴에 못 움직인다"),
    "잡기":       (None,    "붙잡는다. 상대는 탈출에 성공할 때까지 움직이지 못한다"),
    "탈출":       (None,    "잡히거나 조여진 데서 빠져나온다"),
    "조르기":     ("1D6",   "목을 조른다. 잡은 상태가 유지되는 한 매 턴 피해가 들어간다"),
    "넘어뜨리기": (None,    "다리를 걸어 쓰러뜨린다. 넘어진 상대는 맞히기 쉬워진다"),
    "그래플":     ("1D6",   "엉겨붙어 제압한다. 피해를 주면서 상대를 묶어둔다"),
}


def 격투판정(user_id, 기술="타격", 유파=None, 대상=None, channel_id=None, 보정=0):
    c = 캐릭터(user_id)
    if c is None:
        return None, "캐릭터가 없습니다."
    if 기술 not in 격투기술:
        return None, "기술: " + ", ".join(격투기술)

    유파 = 유파 or _내유파(user_id)
    유파보너스 = 0
    if 유파 and 유파 in 마셜아츠:
        유파보너스 = 마셜아츠[유파][1].get(기술, 0)

    스킬lv = c["스킬"].get("무술", c["스킬"].get("맨손격투", 0))
    눈, 기록, 대성공, 펌블 = 판정주사위()
    총합 = 눈 + 능력(c, "REF") + 스킬lv + 유파보너스 + int(보정 or 0)

    r = {
        "이름": c["name"], "기술": 기술, "유파": 유파, "유파보너스": 유파보너스,
        "주사위": 눈, "기록": 기록, "REF": 능력(c, "REF"), "스킬레벨": 스킬lv,
        "총합": 총합, "대성공": 대성공, "펌블": 펌블, "피해": None,
    }

    목표 = 15
    if 대상 and channel_id:
        n = NPC(channel_id, str(대상))
        if n:
            목표 = 10 + n["ref"] + n["skill"] // 2
            r["대상"] = n["name"]
    r["목표"] = 목표
    r["성공"] = 총합 >= 목표

    if r["성공"]:
        피해식 = 격투기술[기술][0]
        if 피해식:
            # 체격이 클수록 더 아프다
            _, btm = 체격정보(c["stat_body"])
            추가 = abs(btm)
            피해량, 눈들, _ = 굴림(피해식)
            피해량 += 추가
            # 사이버팔이 있으면 맨손 피해가 올라간다
            if "사이버팔" in 설치목록(user_id) and 기술 in ("타격", "그래플"):
                추가피해, _, _ = 굴림("2D6")
                피해량 = max(피해량, 추가피해 + 추가)
            r["피해"] = {"식": 피해식, "굴림": 눈들, "체격보너스": 추가, "합계": 피해량}
            if 대상 and channel_id and r.get("대상"):
                부위눈, 부위이름, 칸 = 부위판정()
                n = NPC(channel_id, r["대상"])
                실제, 줄 = 피해계산(n, 피해량, 칸, 부위이름)
                r["피해"]["부위"] = 부위이름
                r["피해"]["실피해"] = 실제
                r["피해"]["설명"] = 줄
                if 실제 > 0:
                    적용, _ = 피해적용(channel_id, r["대상"], 실제, NPC여부=True)
                    r["대상상태"] = 적용
    return r, None


def _내유파(user_id):
    cursor.execute("SELECT name FROM trpg_gear WHERE user_id = ? AND kind = 'martial' LIMIT 1", (str(user_id),))
    r = cursor.fetchone()
    return r[0] if r else None


def 유파배우기(user_id, 유파):
    c = 캐릭터(user_id)
    if c is None:
        return False, "캐릭터가 없습니다."
    if 유파 not in 마셜아츠:
        return False, "유파: " + ", ".join(마셜아츠)
    난이도, 보너스, 설명 = 마셜아츠[유파]
    cursor.execute("DELETE FROM trpg_gear WHERE user_id = ? AND kind = 'martial'", (c["_uid"],))
    cursor.execute("INSERT INTO trpg_gear (user_id, kind, name, qty) VALUES (?, 'martial', ?, 1)",
                   (c["_uid"], 유파))
    if "무술" not in c["스킬"]:
        cursor.execute("INSERT OR REPLACE INTO trpg_skills (user_id, name, level) VALUES (?, '무술', 2)", (c["_uid"],))
    conn.commit()
    보너스글 = ", ".join(f"{k} +{v}" for k, v in 보너스.items())
    return True, f"🥋 **{유파}** (난이도 {난이도})\n{설명}\n보너스: {보너스글}"


def 격투글(r):
    줄 = [f"🥋 **{r['이름']}** — {r['기술']}" + (f" ({r['유파']})" if r["유파"] else "")]
    식 = f"1D10({r['기록']}) + REF {r['REF']} + 무술 {r['스킬레벨']}"
    if r["유파보너스"]:
        식 += f" + 유파 {r['유파보너스']:+d}"
    줄.append(f"{식} = **{r['총합']}** vs {r['목표']} → " + ("✅ 성공" if r["성공"] else "❌ 실패"))
    피해 = r.get("피해")
    if 피해:
        if "설명" in 피해:
            줄.append(f"  · {피해['설명']}")
        else:
            줄.append(f"  피해 {피해['식']} + 체격 {피해['체격보너스']} = **{피해['합계']}**")
    상태 = r.get("대상상태")
    if 상태:
        줄.append(f"**{상태['이름']}** 누적 {상태['누적피해']} — {상태['상태']}")
        s = 상태.get("스턴세이브")
        if s:
            줄.append("  스턴 세이브 → " + ("버텼다" if s["성공"] else "**기절**"))
    return "\n".join(줄)


# ============================================================
# RPG 연동
# ============================================================

RPG_환율 = 10          # RPG 골드 1 = 10eb

# RPG.py 는 연결과 커서를 하나만 두고 모든 곳에서 같이 쓴다.
# 거기에 여러 스레드가 동시에 손대면 sqlite 가 깨지므로, RPG 를 건드리는 동안은 이 잠금을 잡는다.
_RPG잠금 = threading.RLock()


def _RPG():
    import RPG
    return RPG


def RPG캐릭터읽기(user_id):
    try:
        R = _RPG()
    except Exception:
        return None
    try:
        with _RPG잠금:
            return R.캐릭터(str(user_id))
    except Exception:
        return None


def RPG연동생성(user_id, 역할, 이름=None, 라이프패스=True):
    """
    RPG 캐릭터의 '이름만' 가져와서 TRPG 캐릭터를 만든다.
    능력치는 RPG 와 아무 상관 없이 CP2020 규칙대로 새로 굴린다.
    """
    user_id = str(user_id)
    if 캐릭터(user_id) is not None:
        return False, "이미 TRPG 캐릭터가 있습니다. `!TRPG 삭제` 후 다시 하세요."
    rc = RPG캐릭터읽기(user_id)
    if rc is None:
        return False, "RPG 캐릭터가 없습니다. 먼저 `!RPG 생성 [이름]` 으로 만들어주세요."

    이름 = 이름 or rc["name"]
    ok, r = 생성(user_id, 이름, 역할)
    if not ok:
        return False, r

    cursor.execute("UPDATE trpg_characters SET linked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    if 라이프패스:
        라이프패스저장(user_id, 라이프패스생성())

    return True, (f"🔗 RPG 캐릭터 **{rc['name']}** 의 이름으로 만들었습니다.\n"
                  f"능력치는 CP2020 규칙대로 새로 굴렸습니다 (총 {포인트_기본}점). "
                  f"RPG 스탯과는 아무 관계가 없습니다.\n"
                  f"`!TRPG 능력치` 로 직접 다시 배분할 수 있고, 과거도 같이 굴렸습니다.")


def 골드환전(user_id, 방향, 수량):
    """RPG 골드 ↔ TRPG 유로달러"""
    c = 캐릭터(user_id)
    if c is None:
        return False, "TRPG 캐릭터가 없습니다."
    rc = RPG캐릭터읽기(user_id)
    if rc is None:
        return False, "RPG 캐릭터가 없습니다."
    try:
        수량 = int(수량)
    except (TypeError, ValueError):
        return False, "수량은 숫자여야 합니다."
    if 수량 <= 0:
        return False, "1 이상을 넣어주세요."

    R = _RPG()
    if 방향 in ("골드→eb", "eb구매", "buy", "to_eb"):
        eb = 수량 * RPG_환율
        with _RPG잠금:
            R.cursor.execute("UPDATE rpg_characters SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                             (수량, str(user_id), 수량))
            if R.cursor.rowcount == 0:
                R.conn.rollback()
                return False, f"골드가 부족합니다. (보유 {rc['gold']:,})"
            R.conn.commit()
        try:
            cursor.execute("UPDATE trpg_characters SET money = money + ? WHERE user_id = ?", (eb, c["_uid"]))
            conn.commit()
        except Exception:
            # 골드는 이미 빠졌는데 eb 를 못 넣었다. 골드를 돌려준다.
            with _RPG잠금:
                R.cursor.execute("UPDATE rpg_characters SET gold = gold + ? WHERE user_id = ?", (수량, str(user_id)))
                R.conn.commit()
            return False, "환전 중 문제가 생겨 되돌렸습니다. 다시 시도해주세요."
        return True, f"골드 {수량:,} → **{eb:,}eb**. 지금 {캐릭터(user_id)['money']:,}eb"

    if 방향 in ("eb→골드", "골드구매", "sell", "to_gold"):
        필요 = 수량 * RPG_환율
        if not _돈차감(c["_uid"], 필요):
            return False, f"eb 가 부족합니다. {필요:,}eb 필요 (보유 {c['money']:,}eb)"
        conn.commit()
        try:
            with _RPG잠금:
                R.cursor.execute("UPDATE rpg_characters SET gold = gold + ? WHERE user_id = ?", (수량, str(user_id)))
                R.conn.commit()
        except Exception:
            # eb 는 이미 빠졌는데 골드를 못 넣었다. eb 를 돌려준다.
            cursor.execute("UPDATE trpg_characters SET money = money + ? WHERE user_id = ?", (필요, c["_uid"]))
            conn.commit()
            return False, "환전 중 문제가 생겨 되돌렸습니다. 다시 시도해주세요."
        return True, f"{필요:,}eb → **골드 {수량:,}**. 지금 골드 {rc['gold'] + 수량:,}"

    return False, "방향은 '골드→eb' 또는 'eb→골드' 입니다."



# ============================================================
# 설명 — 각 시스템이 무엇이고 어떻게 굴러가는지
# ============================================================

# 본문은 아래에서 주제별로 채운다
설명 = {}
설명["시작"] = """**처음이라면 이 순서대로**
1. `!TRPG 역할` 로 열 가지 역할을 보고 하나 고른다
2. `!TRPG 생성 [이름] [역할]` — 능력치와 역할 스킬이 자동으로 배분된다
3. `!TRPG 시트` 로 내 캐릭터를 확인한다
4. `!TRPG 과거` 로 이 인물이 어떻게 살아왔는지 본다
5. `!TRPG 상점` 에서 무기와 방어구를 산다

**이 게임이 어떤 게임이냐면**
2020년, 기업이 나라보다 센 도시에서 살아남는 이야기다.
주사위 하나(1D10)에 능력치와 스킬을 더해서 목표 숫자를 넘기면 성공이다.
전투는 짧고 잔인하다. 총 한 방에 죽을 수 있으니 방어구를 꼭 입어라.

**더 알고 싶으면** `!TRPG 설명 [주제]`
주제: 판정 / 능력치 / 스킬 / 역할 / 전투 / 부상 / 장비 / 사이버웨어 / 넷러닝 / 격투 / 차량 / 과거 / 연동"""

설명["판정"] = """**판정 — 뭔가 시도할 때 쓰는 기본 규칙**

`1D10 + 능력치 + 스킬 + 보정` 이 **난이도 숫자 이상**이면 성공이다.

`10` **쉬움** — 집중하면 거의 된다
`15` **보통** — 훈련받은 사람이면 해낸다
`20` **어려움** — 전문가도 긴장한다
`25` **매우어려움** — 대부분 실패한다
`30` **거의불가능** — 운이 따라야 한다

**주사위가 뒤집히는 두 가지**
• **10이 나오면** 한 번 더 굴려서 더한다. 못 할 일을 해낸다.
• **1이 나오면** 한 번 더 굴려서 뺀다. 단순 실패가 아니라 상황이 나빠진다.

**보정**은 상황이 유리하면 +, 불리하면 − 다.
어두움 −3, 부상 −2~−6, 조준 +1(턴당, 최대 +3) 같은 식이다.

예) `!TRPG 판정 REF 회피 어려움 -2`
→ 1D10 + 반사 + 회피스킬 − 2 가 20 이상이면 피한다."""

설명["능력치"] = """**능력치 9종 — 타고난 바탕**

합계 60점을 나눠 갖고, 하나당 2~10 이다. 높을수록 좋다.

• **INT 지능** — 기억하고 추리하고 알아챈다. 경계·교양 판정의 바탕
• **REF 반사** — 몸이 빠르다. **전투에서 제일 중요하다.** 명중·회피·이니셔티브 전부 여기서 나온다
• **TECH 기술** — 손재주. 고치고 만들고 수술한다
• **COOL 침착** — 배짱. 겁먹지 않고 버티는 힘. 인간성이 떨어질 때 자제력
• **ATTR 매력** — 생김새와 분위기
• **LUCK 행운** — 운. 판정에 미리 떼어 쓸 수 있다
• **MA 이동** — 한 턴에 얼마나 멀리 가나 (MA × 3 미터)
• **BODY 체격** — 힘과 맷집. **받는 피해를 줄이고 세이브 숫자를 정한다.** 낮으면 쉽게 죽는다
• **EMP 공감** — 사람과 통하는 능력. **인간성의 최대치(EMP × 10)를 정한다**

**BODY 가 중요한 이유**
받는 피해에서 체격만큼 빼준다. BODY 10 이면 매번 4씩 덜 아프다.
스턴 세이브와 데스 세이브도 BODY 숫자 이하를 굴려야 하니, 낮으면 자주 기절하고 자주 죽는다.

`!TRPG 능력치 INT=7 REF=8 ...` 로 직접 배분할 수 있다."""

설명["스킬"] = """**스킬 — 배워서 익힌 것**

0~10 이고, 판정할 때 능력치에 더해진다. 스킬 없이 굴리면 그냥 재능으로만 하는 셈이다.

캐릭터를 만들면 역할에 맞는 스킬 9개가 자동으로 깔린다.
`!TRPG 스킬 [이름] [레벨]` 로 조정하고, `!TRPG 스킬` 만 치면 지금 가진 걸 본다.
`!TRPG 스킬목록` 으로 전체 스킬이 뭘 하는지 볼 수 있다.

**전투에서 꼭 필요한 것**
• **회피** — 없으면 날아오는 걸 다 맞는다
• **경계** — 없으면 매복에 당한다
• 쓸 무기에 맞는 스킬 (권총/소총/자동화기/무기격투)
• **응급처치** — 쓰러진 동료를 살린다"""

설명["역할"] = """**역할 10종 — 이 도시에서 뭘 하며 사는가**

역할마다 **특수능력**이 하나씩 있다. 레벨이 있고, 그 역할만 쓸 수 있다.

• 🔫 **솔로** — 전투감각. 이니셔티브와 경계에 레벨만큼 더한다. 싸움 전문가
• 💻 **넷러너** — 인터페이스. 넷에 들어가 시스템을 턴다
• 🎸 **록커보이** — 카리스마. 사람을 선동하고 끌어모은다
• 🔧 **솔로테크** — 임시수리. 부서진 걸 급조해 되살린다
• 💉 **메드테크** — 의료기술. 죽어가는 사람을 붙잡아둔다
• 📰 **미디어** — 신뢰도. 내 보도를 사람들이 믿는다
• 👮 **캅** — 권위. 배지로 상대를 위축시킨다
• 💼 **코퍼** — 자원. 회사 인력과 장비를 끌어다 쓴다
• 🕶️ **픽서** — 뒷거래. 암시장에서 뭐든 구해온다
• 🏍️ **노매드** — 패밀리. 부족을 불러낼 수 있다

역할은 시작 스킬도 정한다. `!TRPG 역할` 로 각 역할의 주요 스킬을 본다."""

설명["전투"] = """**전투 — 순서와 흐름**

**1) 순서 정하기**
`!TRPG 이니시` — 1D10 + REF. 높은 사람부터 행동한다.
솔로는 전투감각 레벨만큼 더 붙는다. `!TRPG 이니시목록` 으로 순서를 본다.

**2) 적 등록** (GM이 한다)
`!TRPG NPC추가 [이름] [BODY] [REF] [스킬] [SP] [무기]`
잡졸은 BODY 4~5 / 스킬 3, 솔로급은 BODY 8 / 스킬 8 / SP 16 쯤이 적당하다.

**3) 쏘기**
`!TRPG 공격 [대상] [무기] [사거리] [보정] [연사]`
`1D10 + REF + 무기스킬 + 무기보정` 이 사거리 목표치 이상이면 맞는다.

`10` **밀착** — 총구가 닿는다
`15` **근거리** — 무기 사거리의 1/4 안
`20` **중거리** — 무기 사거리의 1/2 안
`25` **원거리** — 적힌 사거리까지
`30` **극원거리** — 사거리의 2배

**4) 어디를 맞았나**
1D10 을 굴린다. 1 머리 / 2-4 몸통 / 5-6 팔 / 7-10 다리.
**머리는 피해가 2배다.** 헬멧을 쓰는 이유가 이것이다.

**5) 실제로 얼마나 아픈가**
`굴린 피해 − 방어구 SP − 체격보정` 이 진짜 피해다.
방어구가 다 막으면 피해 0. 장갑재킷(SP 16)이면 어지간한 권총은 안 뚫린다.

**연사**는 목표치를 3씩 넘길 때마다 한 발씩 더 맞는다. 기관단총이 무서운 이유.

**근접전**은 `!TRPG 격투 [기술] [대상]` 을 쓴다."""

설명["부상"] = """**부상과 죽음 — 이 게임이 무서운 이유**

피해는 쌓인다. 누적 피해에 따라 몸이 망가진다.

`1-4` **경상** — 아직 멀쩡하다
`5-8` **중상** — REF·INT·COOL 이 2 떨어진다
`9-12` **위독** — 4 떨어진다. 제대로 싸우기 힘들다
`13-16` **치명 0** — 6 떨어지고 **죽기 시작한다**
`17-20` **치명 1** — 매 턴 데스 세이브를 굴린다
`21-40` **치명 2~6** — 갈수록 살아남기 어렵다
`41+` **사망**

**스턴 세이브** — 맞을 때마다 굴린다.
`1D10` 이 `BODY − 부상페널티` **이하**면 버틴다. 실패하면 **기절해서 행동 못 한다.**
아픔과 충격 때문에 쓰러지는 것이라, 피해가 적어도 운 나쁘면 넘어간다.

**데스 세이브** — 치명 상태일 때 굴린다.
`1D10` 이 `BODY − 치명단계` **이하**면 산다. 실패하면 **죽는다.**

**살리는 법**
`!TRPG 안정화 [환자]` — `TECH + 의료스킬 + 1D10` 이 환자의 누적 피해 이상이면 성공.
성공하면 더 이상 데스 세이브를 안 굴려도 된다. 메드테크가 있으면 생존율이 확 올라간다.
`!TRPG 회복 [양]` 으로 피해를 줄인다."""

설명["장비"] = """**장비 — 사고 들고 입는다**

`!TRPG 상점` 으로 목록, `!TRPG 구매 [이름]`, `!TRPG 장착 [이름]` 순으로 쓴다.
돈 단위는 **eb(유로달러)** 다. 시작할 때 1,000eb 를 받는다.

**무기 고르는 법**
• 피해는 `2D6+1` 같은 주사위로 적혀 있다. 숫자가 클수록 아프다
• 권총은 숨기기 쉽고, 소총은 멀리 가고, 기관단총은 연사가 된다
• 사거리(m)를 넘기면 명중 목표치가 올라간다

**방어구가 제일 중요하다**
SP 가 피해에서 그대로 빠진다. SP 16 짜리 장갑재킷이면 권총 한 발(2D6+1, 평균 8)은 아예 안 뚫린다.
다만 부위별로 덮는 곳이 다르다. 재킷은 몸통과 팔만 덮으니 **머리는 헬멧을 따로 써야 한다.**
겹쳐 입으면 두꺼운 쪽에 보너스가 조금 붙는다.
무거운 방어구는 REF 가 떨어진다(중장갑재킷 −1, 메탈기어 −2)."""

설명["사이버웨어"] = """**사이버웨어 — 몸을 기계로 바꾼다**

`!TRPG 사이버목록` 으로 보고 `!TRPG 사이버설치 [이름]` 으로 넣는다.
돈이 들고, **인간성이 깎인다.**

**인간성이 뭐냐면**
사람으로 남아 있는 정도다. 최대치는 `EMP × 10` 이다.
설치할 때마다 주사위를 굴려 깎이고, **10 깎일 때마다 EMP 가 1 떨어진다.**
EMP 가 떨어지면 사람을 상대하는 스킬이 같이 약해진다.

**0 이 되면 사이버사이코다.** 사람 얼굴이 고깃덩이로 보이고, 캐릭터를 잃는다.
인간성이 낮아지면 `!TRPG 자제력` 으로 COOL 판정을 해서 버텨야 한다.

**그래서 뭘 넣을까**
• **반사부스터** — REF 가 오른다. 전투에서 가장 체감이 크다
• **피부직조 / 서브더멀아머** — 전신에 SP 가 붙는다. 옷 속에 숨겨진 방어구
• **사이버팔** — 맨손 피해가 2D6 으로 뛴다
• **인터페이스플러그** — 넷러닝과 차량 직결에 필수
• **타겟스코프 / 증폭청각** — 명중과 경계에 보너스

몸이 강해지는 만큼 사람에게서 멀어진다. 그게 이 게임의 핵심이다."""

설명["넷러닝"] = """**넷러닝 — 전산망에 들어가 시스템을 턴다**

넷러너가 하는 일이다. 몸은 의자에 앉아 있고 정신만 들어간다.

**흐름**
1. GM이 `!TRPG 넷생성 [이름] [난이도]` 로 시스템을 만든다
2. 시스템은 **노드** 여러 개로 되어 있고, 노드마다 **ICE**(경비 프로그램)가 붙어 있다
3. `!TRPG 넷침입 [시스템] [프로그램]` 으로 한 번에 노드 하나씩 뚫는다
4. 마지막 CPU 노드까지 뚫으면 시스템을 장악한다

**중요한 것 — 프로그램이 전부다**
ICE 와의 싸움은 `1D10 + 프로그램 강도 + 인터페이스÷2` 대 `1D10 + ICE 강도 + 시스템 등급` 이다.
**능력치 싸움이 아니라 프로그램끼리의 힘겨루기다.**
프로그램 없이 덤비면 거의 진다. `!TRPG 넷상점` 에서 먼저 사라.

• **해머 / 잭해머 / 소드** — ICE 를 부순다. 강도가 높을수록 센 ICE 를 이긴다
• **실드 / 아머** — 반격 피해를 줄여준다. 목숨값이다
• **씨야리얼 / 스피드트랩** — 숨은 것을 드러낸다

**죽을 수 있다**
실패하면 ICE 가 반격한다. 그중 **블랙 ICE**(헬하운드, 리퍼)는 뇌를 직접 태운다.
이 피해는 진짜 피해라 그대로 쌓이고, 치명 상태가 되면 의자에 앉은 채로 죽는다."""

설명["격투"] = """**격투 — 맨손과 근접**

`!TRPG 격투 [기술] [대상]` 으로 굴린다. `1D10 + REF + 무술 + 유파 보너스` 다.

**기술 10종**
• **타격** 1D6 — 주먹
• **발차기** 1D6+2 — 발. 더 아프다
• **막기 / 회피** — 상대 공격을 받아내거나 피한다
• **던지기** 1D6 — 넘긴다. 상대는 다음 턴 행동 불가
• **잡기 / 탈출** — 붙잡고, 빠져나온다
• **조르기** 1D6 — 매 턴 피해
• **넘어뜨리기** — 다리를 걸어 쓰러뜨린다
• **그래플** 1D6 — 엉겨붙어 제압한다

피해에는 체격 보정이 더해진다. 덩치가 클수록 아프게 때린다.
**사이버팔**이 있으면 타격·그래플 피해가 2D6 으로 뛴다.

**유파를 배우면** 기술마다 보너스가 붙는다. `!TRPG 유파` 로 11가지를 보고
`!TRPG 유파배우기 [유파]` 로 하나만 배운다.
• 때리는 걸 원하면 **복싱**(타격+3) / **무에타이**(타격·발차기+3) / **태권도**
• 피하고 넘기려면 **합기도**(막기+4, 회피+3)
• 붙잡아 제압하려면 **레슬링**(잡기·탈출·그래플+4)
• 발차기만 보면 **사바테**(발차기+4)"""

설명["차량"] = """**차량 — 몰고 쫓고 도망친다**

`!TRPG 차량상점` → `!TRPG 차량구매 [이름]` 으로 산다.
`!TRPG 운전 [상황] [차량]` 으로 굴린다. `1D10 + REF + 운전스킬` 이다.

`10` 평범한 주행
`15` 빗길·야간
`20` 추격전
`25` 곡예 주행
`30` 불가능한 묘기

**크게 실패하면 충돌한다.** 빠른 차일수록 피해가 크다.
충돌 피해는 자동으로 굴려지지만 적용은 따로 한다(`!TRPG 피해`).

차량에는 **내구(SDP)** 와 **장갑(SP)** 이 있다.
총을 맞으면 SP 만큼 빼고 SDP 가 깎인다. 0 이 되면 폐차다.
장갑리무진(SDP 80 / SP 30)은 어지간한 총격을 버티고, 오토바이(SDP 20 / SP 5)는 금방 박살난다."""

설명["과거"] = """**라이프패스 — 이 인물이 살아온 길**

`!TRPG 과거` 로 본다. 캐릭터를 만들 때 자동으로 굴려진다.

출신지와 쓰는 말, 입는 옷과 머리 모양, 집안 사정, 어디서 자랐는지,
성격과 가치관, 사람을 어떻게 보는지, 그리고 **열일곱 살부터 해마다 무슨 일이 있었는지** 가 나온다.

빚을 졌거나, 적이 생겼거나, 연인을 잃었거나, 몸에 금속을 심었거나.
**이게 그냥 장식이 아니다.** GM 은 여기서 NPC 와 사건을 끌어다 쓴다.
과거에 생긴 적이 어느 날 문 앞에 서 있는 식이다.

마음에 안 들면 `!TRPG 과거 굴리기` 로 다시 굴린다."""

설명["연동"] = """**RPG 와의 관계**

디스코드 RPG 와 TRPG 는 **별개의 게임이다.** 능력치를 공유하지 않는다.
RPG 는 4스탯(0~25)에 자동 전투고, TRPG 는 9능력치(2~10)에 GM 이 굴리는 테이블탑이라
눈금 자체가 다르기 때문이다.

이어져 있는 건 두 가지뿐이다.
• `!TRPG 연동생성 [역할]` — RPG 캐릭터의 **이름만** 가져온다. 능력치는 새로 굴린다
• `!TRPG 환전 [골드→eb/eb→골드] [수량]` — 돈을 옮긴다 (골드 1 = 10eb)

RPG 에서 아무리 강해져도 TRPG 능력치는 그대로다. 두 캐릭터는 따로 큰다."""

설명["GM"] = """**GM 을 맡은 사람에게**

**AI 에게 맡기려면** AI 프롬프트에 "판정은 반드시 툴로 굴려라" 를 꼭 적어둬야 한다.
안 적으면 AI 가 주사위를 건너뛰고 혼자 결과를 지어낸다.

**굴릴 일과 안 굴릴 일을 갈라라.**
실패해도 이야기가 굴러가는 일만 굴린다. 실패하면 멈춰버리는 일은 그냥 성공시켜라.
문 열 때마다 판정을 시키면 게임이 지루해진다.

**난이도를 먼저 말해줘라.** "이건 어려움, 20이야" 라고 알려주면
플레이어가 보정을 벌 궁리를 한다. 그게 재미있는 부분이다.

**펌블은 그냥 실패가 아니다.** 탄이 걸리고, 들키고, 붙잡힌다.
상황을 한 단계 나쁘게 만들어라.

**적은 미리 등록해라.** `!TRPG NPC추가 [이름] [BODY] [REF] [스킬] [SP]`
• 잡졸 — BODY 4~5 / 스킬 3 / SP 0~4
• 경비 — BODY 6 / 스킬 5 / SP 10~16
• 솔로급 — BODY 8 / 스킬 8 / SP 16~20
• 보스 — BODY 9~10 / 스킬 9 / SP 20+

**캐릭터는 죽는다. 그게 설계다.** 봐주지 마라.
대신 **죽을 수 있다는 걸 미리 알려줘라.** "저 녀석 팔에 뭔가 박혀 있다" 같은 묘사로
경고를 주고, 도망칠 선택지를 항상 남겨두면 된다."""

설명_별칭 = {
    "도움말": "시작", "처음": "시작", "help": "시작", "start": "시작",
    "roll": "판정", "주사위": "판정", "난이도": "판정",
    "stat": "능력치", "스탯": "능력치",
    "skill": "스킬", "스킬표": "스킬",
    "role": "역할", "직업": "역할",
    "combat": "전투", "싸움": "전투", "공격": "전투", "사격": "전투",
    "wound": "부상", "피해": "부상", "죽음": "부상", "세이브": "부상", "치료": "부상",
    "gear": "장비", "무기": "장비", "방어구": "장비", "상점": "장비",
    "cyber": "사이버웨어", "인간성": "사이버웨어", "사이버": "사이버웨어",
    "net": "넷러닝", "넷": "넷러닝", "해킹": "넷러닝", "ice": "넷러닝",
    "melee": "격투", "무술": "격투", "유파": "격투", "마셜아츠": "격투",
    "vehicle": "차량", "운전": "차량", "추격": "차량",
    "lifepath": "과거", "라이프패스": "과거",
    "link": "연동", "rpg": "연동", "환전": "연동",
}


def 설명보기(주제=None):
    if not 주제:
        return 설명["시작"]
    키 = str(주제).strip()
    키 = 설명_별칭.get(키.lower(), 키)
    if 키 in 설명:
        return 설명[키]
    return ("그런 주제가 없습니다. 쓸 수 있는 주제:\n"
            + " / ".join(설명) + "\n\n`!TRPG 설명` 만 치면 처음 안내가 나옵니다.")


def 스킬설명(이름=None):
    if 이름:
        d = 스킬표.get(이름)
        if d is None:
            return f"'{이름}' 스킬을 찾을 수 없습니다."
        return f"**{이름}** ({d[0]} 로 굴림)\n{d[1]}"
    묶음 = {}
    for n, (st, ds) in 스킬표.items():
        묶음.setdefault(st, []).append(f"{n} — {ds}")
    줄 = []
    for st in 능력치순서:
        if st in 묶음:
            줄.append(f"**{st} ({능력치표[st][0]}) 로 굴리는 스킬**")
            줄 += [f"  {x}" for x in sorted(묶음[st])]
    return "\n".join(줄)


# ============================================================
# 디스코드 출력
# ============================================================

def 캐릭터카드(c):
    이모지, 특수, 특수설명, _ = 역할표[c["role"]]
    체격이름, btm = 체격정보(c["stat_body"])
    상태, 스턴, 치명 = 부상정보(c["damage"])

    embed = discord.Embed(
        title=f"{이모지} {c['name']}",
        description=f"**{c['role']}** · {특수} Lv.{c['special']} · {c['money']:,}eb",
        color=0xFF003C,
    )
    능력줄 = " · ".join(f"{k} {c[f'stat_{k.lower()}']}" for k in 능력치순서)
    embed.add_field(name="능력치", value=능력줄, inline=False)

    현재REF = 능력(c, "REF")
    embed.add_field(
        name="상태",
        value=(f"누적 피해 **{c['damage']}** → {상태}\n"
               f"스턴 세이브 {max(0, c['stat_body'] + 스턴)} 이하 · 체격 {체격이름}(BTM {btm})\n"
               + (f"⚠️ 데스 세이브 {c['stat_body'] - 치명} 이하\n" if 치명 is not None else "")
               + (f"REF 가 {c['stat_ref']} → {현재REF} 로 떨어짐" if 현재REF != c["stat_ref"] else "")),
        inline=False,
    )
    embed.add_field(name="인간성", value=f"{인간성(c)} / {최대인간성(c)}", inline=True)
    무기이름 = c["weapon"] or "주먹"
    if 무기이름 in 무기:
        _, 스킬명, wa, 피해식, _, 사거리m, _, _ = 무기[무기이름]
        embed.add_field(name="무기", value=f"{무기이름} ({피해식}, {사거리m}m)", inline=True)
    방어이름 = c["armor"]
    if 방어이름 and 방어이름 in 방어구:
        sp, 부위들, _, _ = 방어구[방어이름]
        embed.add_field(name="방어구", value=f"{방어이름} SP {sp}", inline=True)

    스킬 = c["스킬"]
    if 스킬:
        위 = sorted(스킬.items(), key=lambda x: (-x[1], x[0]))[:12]
        embed.add_field(name="스킬", value=" · ".join(f"{n} {lv}" for n, lv in 위), inline=False)
    embed.set_footer(text=f"{특수}: {특수설명}")
    return embed


def 판정문장(r):
    머리 = "✅ 성공" if r["성공"] else "❌ 실패"
    if r["대성공"]:
        머리 = "💥 대성공! " + 머리
    if r["펌블"]:
        머리 = "💀 펌블! " + 머리
    return (f"**{r['이름']}** — {r['식']}\n"
            f"= **{r['총합']}** vs {r['난이도']} {r['목표']} → {머리}")


def 공격문장(r):
    줄 = [f"🎯 **{r['공격자']}** → **{r['대상']}** ({r['무기']}, {r['사거리']})",
          f"{r['식']} = **{r['총합']}** vs {r['목표']}"]
    if r["펌블"]:
        줄.append("💀 **펌블!** 무기를 놓치거나 탄이 걸렸다 (GM 판단)")
    if not r["명중"]:
        줄.append("❌ **빗나감**")
        return "\n".join(줄)
    if r["대성공"]:
        줄.append("💥 **대성공!**")
    줄.append(f"✅ **명중** ({r.get('발수', 1)}발)")
    for t in r["타격"]:
        줄.append(f"  · {t['설명']}")
    줄.append(f"**총 피해 {r['총피해']}**")

    상태 = r.get("대상상태")
    if 상태:
        줄.append(f"**{상태['이름']}** 누적 {상태['누적피해']} — {상태['상태']}")
        s = 상태.get("스턴세이브")
        if s:
            줄.append(f"  스턴 세이브 {s['주사위']} vs {s['목표']} → "
                      + ("버텼다" if s["성공"] else "**기절·행동 불가**"))
        d = 상태.get("데스세이브")
        if d:
            줄.append(f"  💀 데스 세이브 {d['주사위']} vs {d['목표']} → "
                      + ("살아있다" if d["성공"] else "**사망**"))
    return "\n".join(줄)


# ============================================================
# 명령어
# ============================================================

도움말 = """**CP2020 TRPG** — 2020년 나이트 시티에서 살아남는 이야기

📖 **처음이면 `!TRPG 설명` 부터 읽어보세요.**
`!TRPG 설명 [주제]` — 판정/능력치/스킬/역할/전투/부상/장비/사이버웨어/넷러닝/격투/차량/과거/연동
`!TRPG 스킬목록` — 스킬 42종이 각각 뭘 하는지

**캐릭터**
`!TRPG 생성 [이름] [역할]` — 캐릭터 만들기 (능력치 자동 배분)
`!TRPG 역할` — 역할 목록
`!TRPG 시트` — 내 캐릭터 보기
`!TRPG 능력치 INT=7 REF=8 ...` — 능력치 직접 배분
`!TRPG 스킬 [이름] [레벨]` — 스킬 설정
`!TRPG 판정 [능력치] [스킬] [난이도] [보정]` — 예: `!TRPG 판정 REF 회피 어려움`
`!TRPG 공격 [대상] [무기] [사거리] [보정] [연사]`
`!TRPG 이니시` / `!TRPG 이니시목록` / `!TRPG 이니시초기화`
`!TRPG NPC추가 [이름] [BODY] [REF] [스킬] [SP] [무기]`
`!TRPG NPC목록` / `!TRPG NPC정리`
`!TRPG 피해 [대상] [양]` / `!TRPG 회복 [양]` / `!TRPG 안정화 [환자]`
`!TRPG 상점` / `!TRPG 구매 [이름] [수량]` / `!TRPG 장착 [이름]`
`!TRPG 삭제`

**과거와 몸**
`!TRPG 과거` — 라이프패스 보기 (`!TRPG 과거 굴리기` 로 새로 굴림)
`!TRPG 사이버목록` / `!TRPG 사이버설치 [이름]` / `!TRPG 사이버제거 [이름]`
`!TRPG 자제력` — 인간성이 낮을 때 COOL 판정

**넷러닝**
`!TRPG 넷상점` / `!TRPG 프로그램 [이름] [수량]`
`!TRPG 넷생성 [시스템이름] [난이도]` — GM이 침입할 시스템을 만든다
`!TRPG 넷침입 [시스템] [프로그램]` / `!TRPG 넷목록` / `!TRPG 넷정리`

**차량과 격투**
`!TRPG 차량상점` / `!TRPG 차량구매 [이름]` / `!TRPG 운전 [상황] [차량]`
`!TRPG 유파` — 마셜아츠 유파 목록 / `!TRPG 유파배우기 [유파]`
`!TRPG 격투 [기술] [대상]` — 기술: 타격/발차기/막기/회피/던지기/잡기/탈출/조르기/넘어뜨리기/그래플

**RPG 연동**
`!TRPG 연동생성 [역할]` — RPG 캐릭터 이름으로 만든다 (능력치는 새로 굴림)
`!TRPG 환전 [골드→eb/eb→골드] [수량]`"""


async def 명령어(ctx, 용도=None, 인자=()):
    uid = str(ctx.author.id)
    ch = str(ctx.channel.id)
    인자 = list(인자)

    if 용도 is None or 용도 in ("도움말", "help"):
        await ctx.reply(도움말)
        return

    if 용도 in ("설명", "가이드", "규칙"):
        글 = 설명보기(인자[0] if 인자 else None)
        for i in range(0, len(글), 1900):
            await ctx.send(글[i:i + 1900])
        return

    if 용도 in ("스킬목록", "스킬표", "스킬설명"):
        글 = 스킬설명(인자[0] if 인자 else None)
        머리 = "**스킬 42종** — 판정할 때 괄호 안 능력치에 더해집니다\n\n"
        글 = 머리 + 글 if not 인자 else 글
        for i in range(0, len(글), 1900):
            await ctx.send(글[i:i + 1900])
        return

    if 용도 == "역할":
        embed = discord.Embed(title="역할 고르기", color=0xFF003C)
        embed.description = ("역할은 이 도시에서 뭘 하며 사는가입니다. 역할마다 **특수능력**이 하나씩 있고, "
                             "시작 스킬도 역할이 정합니다.")
        for 이름, (이모지, 특수, 설명문, 스킬들) in 역할표.items():
            embed.add_field(name=f"{이모지} {이름} — 특수능력 「{특수}」",
                            value=f"{설명문}\n시작 스킬: {', '.join(스킬들)}", inline=False)
        embed.set_footer(text="!TRPG 생성 [이름] [역할] · 자세한 건 !TRPG 설명 역할")
        await ctx.send(embed=embed)
        return

    if 용도 == "생성":
        if len(인자) < 2:
            await ctx.reply("사용법: `!TRPG 생성 [이름] [역할]` — 역할은 `!TRPG 역할` 로 확인")
            return
        ok, r = 생성(uid, 인자[0], 인자[1])
        if not ok:
            await ctx.reply(r)
            return
        await ctx.send(f"**{r['name']}** 등장. 나이트 시티에 오신 걸 환영합니다.", embed=캐릭터카드(r))
        return

    if 용도 in ("시트", "캐릭터", "정보"):
        c = 캐릭터(uid)
        if c is None:
            await ctx.reply("캐릭터가 없습니다. `!TRPG 생성 [이름] [역할]`")
            return
        await ctx.send(embed=캐릭터카드(c))
        return

    if 용도 == "능력치":
        c = 캐릭터(uid)
        if c is None:
            await ctx.reply("캐릭터가 없습니다.")
            return
        값 = {}
        for 조각 in 인자:
            if "=" in 조각:
                k, v = 조각.split("=", 1)
                k = 능력치_별칭.get(k.strip().lower(), k.strip().upper())
                값[k] = v.strip()
        if len(값) != len(능력치순서):
            await ctx.reply(f"9개 전부 적어주세요. 예: `!TRPG 능력치 INT=7 REF=8 TECH=5 COOL=6 ATTR=5 LUCK=6 MA=7 BODY=8 EMP=8`\n합계 {포인트_기본} 이하, 각 {능력치_최소}~{능력치_최대}")
            return
        ok, r = 능력치_검사(값)
        if not ok:
            await ctx.reply(r)
            return
        cursor.execute(
            "UPDATE trpg_characters SET stat_int=?, stat_ref=?, stat_tech=?, stat_cool=?, "
            "stat_attr=?, stat_luck=?, stat_ma=?, stat_body=?, stat_emp=? WHERE user_id=?",
            (r["INT"], r["REF"], r["TECH"], r["COOL"], r["ATTR"], r["LUCK"], r["MA"], r["BODY"], r["EMP"], uid))
        conn.commit()
        await ctx.send("능력치를 설정했습니다.", embed=캐릭터카드(캐릭터(uid)))
        return

    if 용도 == "스킬":
        if len(인자) < 2:
            c = 캐릭터(uid)
            if c is None:
                await ctx.reply("캐릭터가 없습니다.")
                return
            스킬 = c["스킬"]
            if not 스킬:
                await ctx.reply("가진 스킬이 없습니다. `!TRPG 스킬 [이름] [레벨]` 로 올리세요.")
                return
            줄 = ["**내 스킬** — 판정할 때 괄호 안 능력치에 더해집니다"]
            for n, lv in sorted(스킬.items(), key=lambda x: (-x[1], x[0])):
                d = 스킬표.get(n)
                줄.append(f"`{lv}` **{n}** ({d[0]})" + (f" — {d[1]}" if d else ""))
            줄.append("\n설정: `!TRPG 스킬 [이름] [레벨]` · 전체 목록: `!TRPG 스킬목록`")
            await ctx.reply("\n".join(줄)[:1950])
            return
        ok, msg = 스킬설정(uid, 인자[0], 인자[1])
        await ctx.reply(msg)
        return

    if 용도 == "판정":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 판정 [능력치] [스킬] [난이도] [보정]`\n예: `!TRPG 판정 REF 회피 어려움`")
            return
        능력치키 = 인자[0]
        스킬 = 인자[1] if len(인자) > 1 else None
        난이도 = 인자[2] if len(인자) > 2 else None
        보정 = 인자[3] if len(인자) > 3 else 0
        r, err = 판정(uid, 능력치키, 스킬, 난이도, 보정)
        await ctx.reply(err or 판정문장(r))
        return

    if 용도 == "공격":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 공격 [대상] [무기] [사거리] [보정] [연사]`")
            return
        대상 = 인자[0]
        무기이름 = 인자[1] if len(인자) > 1 else None
        사거리 = 인자[2] if len(인자) > 2 else "중거리"
        보정 = 인자[3] if len(인자) > 3 else 0
        연사 = 인자[4] if len(인자) > 4 else 1
        r, err = 공격(ch, 공격자_uid=uid, 대상=대상, 무기이름=무기이름,
                     사거리=사거리, 보정=보정, 연사=연사)
        await ctx.reply(err or 공격문장(r))
        return

    if 용도 in ("이니시", "이니셔티브"):
        r, err = 이니시굴림(ch, user_id=uid)
        if err:
            await ctx.reply(err)
            return
        await ctx.reply(f"🎲 **{r['이름']}** 이니셔티브 "
                        f"1D10({r['주사위']}) + REF {r['REF']}"
                        + (f" + {r['보정']}" if r["보정"] else "")
                        + f" = **{r['값']}**")
        return

    if 용도 == "이니시목록":
        목록 = 이니시목록(ch)
        if not 목록:
            await ctx.reply("이니셔티브가 비어 있습니다.")
            return
        await ctx.reply("**턴 순서**\n" + "\n".join(f"{i+1}. {n} — {v}" for i, (n, v) in enumerate(목록)))
        return

    if 용도 == "이니시초기화":
        await ctx.reply(이니시초기화(ch))
        return

    if 용도 == "NPC추가":
        if not 인자:
            await ctx.reply("사용법: `!TRPG NPC추가 [이름] [BODY] [REF] [스킬] [SP] [무기]`\n예: `!TRPG NPC추가 부스터갱 6 6 4 10 중권총`")
            return
        def _i(i, 기본):
            try:
                return int(인자[i])
            except (IndexError, ValueError):
                return 기본
        무기이름 = 인자[5] if len(인자) > 5 else "중권총"
        await ctx.reply(NPC추가(ch, 인자[0], _i(1, 5), _i(2, 5), _i(3, 4), _i(4, 0), 무기이름))
        return

    if 용도 == "NPC목록":
        목록 = NPC목록(ch)
        if not 목록:
            await ctx.reply("등록된 NPC가 없습니다.")
            return
        줄 = []
        for n, b, rf, sk, ar, dmg in 목록:
            상태, _, _ = 부상정보(dmg)
            줄.append(f"**{n}** — BODY {b} / REF {rf} / 스킬 {sk} / SP {ar} / 피해 {dmg} ({상태})")
        await ctx.reply("\n".join(줄))
        return

    if 용도 == "NPC정리":
        await ctx.reply(NPC정리(ch))
        return

    if 용도 == "피해":
        if len(인자) < 2:
            await ctx.reply("사용법: `!TRPG 피해 [대상이름] [양]`")
            return
        대상 = 인자[0]
        try:
            양 = int(인자[1])
        except ValueError:
            await ctx.reply("피해량은 숫자여야 합니다.")
            return
        isnpc = NPC(ch, 대상) is not None
        목표 = 대상 if isnpc else uid
        r, err = 피해적용(ch, 목표, 양, NPC여부=isnpc)
        if err:
            await ctx.reply(err)
            return
        줄 = [f"**{r['이름']}** 피해 {양} → 누적 {r['누적피해']} ({r['상태']})"]
        s = r.get("스턴세이브")
        if s:
            줄.append(f"스턴 세이브 {s['주사위']} vs {s['목표']} → " + ("버텼다" if s["성공"] else "**기절**"))
        d = r.get("데스세이브")
        if d:
            줄.append(f"💀 데스 세이브 {d['주사위']} vs {d['목표']} → " + ("살아있다" if d["성공"] else "**사망**"))
        await ctx.reply("\n".join(줄))
        return

    if 용도 == "회복":
        양 = 인자[0] if 인자 else None
        ok, msg = 회복(uid, 양)
        await ctx.reply(msg)
        return

    if 용도 == "안정화":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 안정화 [환자이름]`")
            return
        대상 = 인자[0]
        isnpc = NPC(ch, 대상) is not None
        r, err = 안정화(uid, 대상 if isnpc else uid, NPC여부=isnpc, channel_id=ch)
        if err:
            await ctx.reply(err)
            return
        await ctx.reply(f"🩺 **{r['의사']}** → {r['환자']}\n"
                        f"1D10({r['주사위']}) + TECH {r['TECH']} + 의료 {r['스킬레벨']} = **{r['총합']}** vs {r['목표']}\n"
                        + ("✅ 안정화 성공. 더 이상 죽지 않는다" if r["성공"] else "❌ 실패. 계속 피를 흘린다"))
        return

    if 용도 == "상점":
        c = 캐릭터(uid)
        embed = discord.Embed(
            title="🏪 나이트 시티 상점",
            description=("**읽는 법** · 무기의 `2D6+1` 은 맞혔을 때 굴리는 피해입니다. "
                         "방어구의 `SP` 는 피해에서 그대로 빠지는 값이고, 덮는 부위가 정해져 있습니다.\n"
                         + (f"보유 {c['money']:,}eb" if c else "")),
            color=0xFF003C)
        무기줄 = [f"**{n}** {d[3]} · {d[5]}m · {d[6]:,}eb" for n, d in list(무기.items())[:14] if d[6] > 0]
        embed.add_field(name="무기 (피해 · 사거리 · 값)", value="\n".join(무기줄), inline=False)
        방어줄 = [f"**{n}** SP {d[0]} · {', '.join(d[1])} · {d[2]:,}eb" for n, d in 방어구.items()]
        embed.add_field(name="방어구 (SP · 덮는 부위 · 값)", value="\n".join(방어줄), inline=False)
        아이템줄 = [f"**{n}** {d[0]:,}eb · {d[1]}" for n, d in 아이템.items()]
        embed.add_field(name="아이템", value="\n".join(아이템줄), inline=False)
        embed.set_footer(text="!TRPG 구매 [이름] → !TRPG 장착 [이름] · 머리는 헬멧을 따로 써야 합니다")
        await ctx.send(embed=embed)
        return

    if 용도 == "구매":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 구매 [이름] [수량]`")
            return
        수량 = 인자[1] if len(인자) > 1 else 1
        ok, msg = 구매(uid, 인자[0], 수량)
        await ctx.reply(msg)
        return

    if 용도 == "장착":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 장착 [무기 또는 방어구]`")
            return
        ok, msg = 장비장착(uid, 인자[0])
        await ctx.reply(msg)
        return

    if 용도 == "소지품":
        목록 = 소지품(uid)
        if not 목록:
            await ctx.reply("가진 게 없습니다.")
            return
        await ctx.reply("\n".join(f"[{k}] {n} ×{q}" for k, n, q in 목록))
        return

    if 용도 == "삭제":
        ok, msg = 삭제(uid)
        await ctx.reply(msg)
        return

    # ---------- 라이프패스 ----------
    if 용도 in ("과거", "라이프패스"):
        if 인자 and 인자[0] in ("굴리기", "재굴림", "다시"):
            if 캐릭터(uid) is None:
                await ctx.reply("캐릭터가 없습니다.")
                return
            d = 라이프패스생성()
            라이프패스저장(uid, d)
            await ctx.reply("과거를 새로 굴렸습니다.\n\n" + 라이프패스글(d))
            return
        await ctx.reply(라이프패스글(라이프패스읽기(uid)))
        return

    # ---------- 사이버웨어 ----------
    if 용도 == "사이버목록":
        c = 캐릭터(uid)
        embed = discord.Embed(title="🔩 사이버웨어 클리닉", color=0xFF003C)
        분류별 = {}
        for 이름, (분류, 손실, 가격, 설명, _) in 사이버웨어.items():
            분류별.setdefault(분류, []).append(f"{이름} — {가격:,}eb · 인간성 {손실} · {설명}")
        for 분류, 줄들 in 분류별.items():
            embed.add_field(name=분류, value="\n".join(줄들), inline=False)
        if c:
            가진 = 설치목록(uid)
            embed.add_field(name="지금 달고 있는 것",
                            value=(", ".join(f"{n}×{q}" if q > 1 else n for n, q in 가진.items()) if 가진 else "없음")
                                  + f"\n인간성 {인간성(c)} / {최대인간성(c)}", inline=False)
        embed.description = ("몸을 기계로 바꿉니다. 돈이 들고 **인간성이 깎입니다.** "
                             "인간성이 10 깎일 때마다 EMP 가 1 떨어지고, 0 이 되면 사이버사이코가 되어 캐릭터를 잃습니다.\n"
                             "표기의 `2D6` 은 설치할 때 굴리는 인간성 손실량입니다.")
        embed.set_footer(text="!TRPG 사이버설치 [이름] · 자세한 건 !TRPG 설명 사이버웨어")
        await ctx.send(embed=embed)
        return

    if 용도 == "사이버설치":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 사이버설치 [이름]`")
            return
        ok, msg = 사이버설치(uid, 인자[0])
        await ctx.reply(msg)
        return

    if 용도 == "사이버제거":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 사이버제거 [이름]`")
            return
        ok, msg = 사이버제거(uid, 인자[0])
        await ctx.reply(msg)
        return

    if 용도 in ("자제력", "사이버사이코"):
        r, err = 사이버사이코판정(uid)
        if err:
            await ctx.reply(err)
            return
        await ctx.reply(f"🧠 **{r['이름']}** 자제력 판정 (남은 인간성 {r['남은인간성']})\n"
                        f"1D10({r['기록']}) + COOL {r['COOL']} = **{r['총합']}** vs {r['목표']} → "
                        + ("✅ 아직 사람이다" if r["성공"] else "❌ **이성을 잃었다.** 가까운 것부터 부순다"))
        return

    # ---------- 넷러닝 ----------
    if 용도 == "넷상점":
        embed = discord.Embed(title="💻 프로그램 상점", color=0x00FFB3)
        분류별 = {}
        for 이름, (분류, 강도, 가격, 설명) in 프로그램.items():
            분류별.setdefault(분류, []).append(f"{이름} (강도 {강도}) — {가격:,}eb · {설명}")
        for 분류, 줄들 in 분류별.items():
            embed.add_field(name=분류, value="\n".join(줄들), inline=False)
        embed.add_field(name="ICE 종류",
                        value="\n".join(f"{n} (강도 {d[1]}) — {d[3]}" for n, d in ICE.items()), inline=False)
        embed.description = ("넷에 들어가 시스템을 털 때 쓰는 프로그램입니다.\n"
                             "**ICE** 는 시스템을 지키는 경비 프로그램입니다. 싸움은 "
                             "`1D10 + 내 프로그램 강도` 대 `1D10 + ICE 강도` 로, **능력치가 아니라 프로그램 강도 싸움**입니다.\n"
                             "프로그램 없이 덤비면 거의 집니다. 공격용 하나와 방어용 하나는 꼭 챙기세요.")
        embed.set_footer(text="!TRPG 프로그램 [이름] 으로 구매 · 자세한 건 !TRPG 설명 넷러닝")
        await ctx.send(embed=embed)
        return

    if 용도 in ("프로그램", "프로그램구매"):
        if not 인자:
            cursor.execute("SELECT name FROM trpg_gear WHERE user_id = ? AND kind = 'program'", (uid,))
            가진 = [r[0] for r in cursor.fetchall()]
            await ctx.reply("가진 프로그램: " + (", ".join(가진) if 가진 else "없음")
                            + "\n구매: `!TRPG 프로그램 [이름]`")
            return
        수량 = 인자[1] if len(인자) > 1 else 1
        ok, msg = 프로그램구매(uid, 인자[0], 수량)
        await ctx.reply(msg)
        return

    if 용도 == "넷생성":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 넷생성 [시스템이름] [쉬움/보통/어려움/매우어려움]`")
            return
        난이도 = 인자[1] if len(인자) > 1 else "보통"
        sysd = 넷시스템생성(ch, 인자[0], 난이도)
        줄 = [f"🏢 **{sysd['이름']}** ({난이도}, 노드 {len(sysd['노드'])}개) 구축 완료"]
        for n in sysd["노드"]:
            줄.append(f"  노드 {n['번호']} [{n['종류']}] — {', '.join(n['ICE']) or '경비 없음'}")
        await ctx.reply("\n".join(줄))
        return

    if 용도 == "넷침입":
        if not 인자:
            목록 = 넷시스템목록(ch)
            await ctx.reply("사용법: `!TRPG 넷침입 [시스템] [프로그램]`\n이 채널의 시스템: "
                            + (", ".join(목록) if 목록 else "없음"))
            return
        프로그램이름 = 인자[1] if len(인자) > 1 else None
        r, err = 넷침입(uid, ch, 인자[0], 프로그램이름)
        await ctx.reply(err or 넷침입글(r))
        return

    if 용도 == "넷목록":
        목록 = 넷시스템목록(ch)
        if not 목록:
            await ctx.reply("이 채널에 시스템이 없습니다.")
            return
        줄 = []
        for 이름 in 목록:
            d = 넷시스템(ch, 이름)
            뚫린 = sum(1 for n in d["노드"] if n["뚫림"])
            줄.append(f"**{이름}** ({d['난이도']}) — {뚫린}/{len(d['노드'])} 노드 돌파, 경보 {d['경보']}회")
            for n in d["노드"]:
                표시 = "✅" if n["뚫림"] else "🔒"
                줄.append(f"  {표시} 노드 {n['번호']} [{n['종류']}] {', '.join(n['ICE']) or '-'}")
        await ctx.reply("\n".join(줄))
        return

    if 용도 == "넷정리":
        await ctx.reply(넷시스템삭제(ch, 인자[0] if 인자 else None))
        return

    # ---------- 차량 ----------
    if 용도 == "차량상점":
        embed = discord.Embed(
            title="🚗 차량 딜러",
            description=("**내구(SDP)** 는 차가 버티는 총량, **장갑(SP)** 은 총에 맞을 때 빠지는 값입니다. "
                         "내구가 0 이 되면 폐차입니다.\n운전은 `!TRPG 운전 [상황] [차량]` 으로 굴립니다."),
            color=0xFF003C)
        for 이름, (분류, sdp, sp, 속도, 정원, 가격, 설명) in 차량.items():
            embed.add_field(name=f"{이름} — {가격:,}eb",
                            value=f"{분류} · 내구 {sdp} · 장갑 {sp} · {속도}km/h · {정원}인승\n{설명}",
                            inline=False)
        await ctx.send(embed=embed)
        return

    if 용도 == "차량구매":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 차량구매 [이름]`")
            return
        ok, msg = 차량구매(uid, 인자[0], 인자[1] if len(인자) > 1 else 1)
        await ctx.reply(msg)
        return

    if 용도 == "운전":
        상황 = 인자[0] if 인자 else "추격전"
        차량이름 = 인자[1] if len(인자) > 1 else None
        r, err = 운전판정(uid, 상황, 차량이름=차량이름)
        if err:
            await ctx.reply(err)
            return
        줄 = [f"🏍️ **{r['이름']}** — {r['상황']}" + (f" ({r['차량']})" if r["차량"] else ""),
              f"1D10({r['기록']}) + REF {r['REF']} + 운전 {r['스킬레벨']} = **{r['총합']}** vs {r['목표']} → "
              + ("✅ 성공" if r["성공"] else "❌ 실패")]
        if r["충돌"]:
            줄.append(f"💥 **충돌!** 피해 {r['충돌']['피해식']} = **{r['충돌']['피해']}**")
        await ctx.reply("\n".join(줄))
        return

    # ---------- 마셜아츠 ----------
    if 용도 == "유파":
        embed = discord.Embed(title="🥋 마셜아츠 유파", color=0xFF003C)
        for 이름, (난이도, 보너스, 설명) in 마셜아츠.items():
            embed.add_field(name=f"{이름} (난이도 {난이도})",
                            value=f"{설명}\n{', '.join(f'{k} +{v}' for k, v in 보너스.items())}",
                            inline=False)
        embed.description = ("맨손 격투의 유파입니다. **하나만 배울 수 있고**, 기술마다 붙는 보너스가 다릅니다.\n"
                             "때리고 싶으면 복싱·무에타이, 피하고 넘기려면 합기도, 붙잡아 제압하려면 레슬링입니다.")
        embed.set_footer(text="!TRPG 유파배우기 [유파] · 쓰는 법은 !TRPG 격투 [기술] [대상]")
        await ctx.send(embed=embed)
        return

    if 용도 == "유파배우기":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 유파배우기 [유파]`")
            return
        ok, msg = 유파배우기(uid, 인자[0])
        await ctx.reply(msg)
        return

    if 용도 == "격투":
        기술 = 인자[0] if 인자 else "타격"
        대상 = 인자[1] if len(인자) > 1 else None
        r, err = 격투판정(uid, 기술, 대상=대상, channel_id=ch)
        await ctx.reply(err or 격투글(r))
        return

    # ---------- RPG 연동 ----------
    if 용도 == "연동생성":
        if not 인자:
            await ctx.reply("사용법: `!TRPG 연동생성 [역할]`\n"
                            "RPG 캐릭터의 이름만 가져옵니다. 능력치는 CP2020 규칙대로 새로 굴립니다.")
            return
        ok, msg = RPG연동생성(uid, 인자[0])
        if not ok:
            await ctx.reply(msg)
            return
        await ctx.send(msg, embed=캐릭터카드(캐릭터(uid)))
        return

    if 용도 == "환전":
        if len(인자) < 2:
            await ctx.reply(f"사용법: `!TRPG 환전 [골드→eb/eb→골드] [수량]`\n환율: 골드 1 = {RPG_환율}eb")
            return
        ok, msg = 골드환전(uid, 인자[0], 인자[1])
        await ctx.reply(msg)
        return

    await ctx.reply(도움말)