"""
실제 한국 주식(코스피·코스닥) 최근 3년 자료를 받아서 주식.py 가 쓰는 시장자료.json 을 만든다.
봇이 돌 때는 필요 없고, 자료를 새로 고치고 싶을 때만 직접 돌린다 (7분쯤 걸림).

    venv/bin/python -I 시장자료_수집.py [받은 원본을 둘 폴더]

받는 것 (네이버 금융 공개 시세)
  - 코스피·코스닥 지수 일봉 3년
  - 시가총액 상위 종목 일봉 3년 (시장마다 150개) + 업종마다 시가총액 상위 종목
  - 업종별 종목 목록 (네이버 업종 79개 → 아래 업종표로 묶는다)
  - 상위 종목 분봉 최근 며칠 (장중 변동 모양용)

만드는 것
  - 지수 : 날짜별 (갭, 장중 수익률, 장중 변동폭) — 엔진이 실제 지수의 하루하루를 뽑아 쓴다
  - 업종·업종날 : 업종마다 날짜별 '지수로 설명 안 되는 업종 공통 움직임' (갭, 장중).
                 지수와 같은 날짜를 쓰기 때문에 "반도체가 끌고 가는 날", "바이오만 빠지는 날"이 실제처럼 나온다
  - 종목 : 실제 종목마다 베타·고유 변동성·업종·업종 민감도 — 봇 종목 하나가 같은 업종 실제 종목 하나의 성격을 물려받는다
  - 잔차 : 실제 종목들의 '지수·업종으로 설명 안 되는 하루 움직임'을 표준화한 표본 (급등·상한가 포함)
  - garch : 변동성이 뭉치는 정도 (어제 크게 움직였으면 오늘도 크게)
  - 장중모양 : 장 시작·끝에 변동이 커지는 U자 모양
"""
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
from datetime import date, timedelta

여기 = os.path.dirname(os.path.abspath(__file__))
원본폴더 = sys.argv[1] if len(sys.argv) > 1 else os.path.join(여기, "시장자료_원본")
결과파일 = os.path.join(여기, "시장자료.json")

종목수 = 150            # 시장마다
분봉종목수 = 25         # 시장마다
최소일수 = 400
표본수 = 15000          # 시장마다 남길 잔차 표본
업종표본수 = 25         # 업종 움직임을 만들 때 쓰는 종목 수 (시가총액 순, 보통주만)
업종_최소종목 = 3       # 그날 이만큼은 거래돼야 업종 움직임으로 친다
업종시장_종목수 = 20    # 시장마다 업종별로 이만큼은 분석한다 (봇 종목이 같은 시장·같은 업종의 실제 종목을 물려받게)
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# 게임 업종 → (설명, 여기에 묶는 네이버 업종 이름들).
# 업종 이름은 봇 명령어에 그대로 치니까 짧고 띄어쓰기 없게 둔다. 고치면 이 파일을 다시 돌려야 한다.
업종표 = {
    "반도체": ("반도체·디스플레이", ("반도체와반도체장비", "디스플레이장비및부품", "디스플레이패널")),
    "전자": ("전자제품·부품·통신장비", ("전자장비와기기", "전자제품", "핸드셋", "컴퓨터와주변기기", "통신장비",
                              "사무용전자제품", "가정용기기와용품")),
    "배터리": ("2차전지·전기제품", ("전기제품",)),
    "IT": ("인터넷·소프트웨어", ("양방향미디어와서비스", "소프트웨어", "IT서비스")),
    "게임": ("게임", ("게임엔터테인먼트",)),
    "엔터": ("방송·연예·광고", ("방송과엔터테인먼트", "광고", "출판")),
    "자동차": ("자동차·부품", ("자동차", "자동차부품")),
    "바이오": ("제약·바이오·의료기기", ("제약", "생물공학", "생명과학도구및서비스", "건강관리장비와용품",
                               "건강관리업체및서비스", "건강관리기술")),
    "화학": ("화학·정유·에너지", ("화학", "석유와가스", "에너지장비및서비스")),
    "광업": ("철강·비철금속", ("철강", "비철금속")),
    "중공업": ("기계·전력기기·방산", ("기계", "전기장비", "우주항공과국방")),
    "조선": ("조선", ("조선",)),
    "건설": ("건설·건자재·부동산", ("건설", "건축자재", "건축제품", "부동산")),
    "금융": ("은행·증권·보험", ("은행", "증권", "손해보험", "생명보험", "카드", "기타금융", "창업투자")),
    "식품": ("식품·음료·외식·레저", ("식품", "음료", "담배", "식품과기본식료품소매", "호텔,레스토랑,레저")),
    "유통": ("백화점·쇼핑·무역", ("백화점과일반상점", "판매업체", "전문소매", "인터넷과카탈로그소매",
                            "무역회사와판매업체")),
    "수공업": ("의류·화장품·가구·생활용품", ("섬유,의류,신발,호화품", "화장품", "가구", "가정용품", "종이와목재",
                                  "포장재", "레저용장비와제품", "문구류")),
    "운송": ("해운·항공·물류", ("해운사", "항공사", "항공화물운송과물류", "도로와철도운송", "운송인프라")),
    "공공": ("전력·가스·통신", ("전기유틸리티", "가스유틸리티", "복합유틸리티", "무선통신서비스",
                          "다각화된통신서비스")),
    "종합": ("지주회사·서비스·교육", ("복합기업", "상업서비스와공급품", "교육서비스", "다각화된소비자서비스")),
}

끝날 = date.today()
시작날 = 끝날 - timedelta(days=365 * 3 + 10)


def 받기(주소, 파일, 인코딩="utf-8"):
    """한 번 받은 건 원본 폴더에 두고 다시 안 받는다"""
    경로 = os.path.join(원본폴더, 파일)
    if os.path.exists(경로) and os.path.getsize(경로) > 50:
        with open(경로, encoding="utf-8") as f:
            return f.read()
    요청 = urllib.request.Request(주소, headers={"User-Agent": UA})
    for 시도 in range(3):
        try:
            with urllib.request.urlopen(요청, timeout=20) as r:
                글 = r.read().decode(인코딩, "replace")
            break
        except Exception as e:
            if 시도 == 2:
                print(f"  실패 {파일}: {e}")
                return ""
            time.sleep(2)
    with open(경로, "w", encoding="utf-8") as f:
        f.write(글)
    time.sleep(0.12)
    return 글


_일봉줄 = re.compile(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)')


def 일봉(코드):
    """[(날짜, 시, 고, 저, 종)] — 거래정지(0원)인 날은 뺀다"""
    글 = 받기(
        "https://api.finance.naver.com/siseJson.naver"
        f"?symbol={코드}&requestType=1&startTime={시작날:%Y%m%d}&endTime={끝날:%Y%m%d}&timeframe=day",
        f"day_{코드}.txt",
    )
    결과 = []
    for d, o, h, l, c, v in _일봉줄.findall(글):
        o, h, l, c = float(o), float(h), float(l), float(c)
        if min(o, h, l, c) > 0 and h >= l:
            결과.append((d, o, h, l, c))
    return 결과


def 상위종목(시장):
    목록 = []
    for 쪽 in range(1, 4):
        글 = 받기(
            f"https://m.stock.naver.com/api/stocks/marketValue/{시장}?page={쪽}&pageSize=100",
            f"list_{시장}_{쪽}.json",
        )
        try:
            항목들 = json.loads(글).get("stocks", [])
        except ValueError:
            항목들 = []
        for s in 항목들:
            코드, 이름 = str(s.get("itemCode", "")), str(s.get("stockName", ""))
            if s.get("stockEndType") == "stock" and re.fullmatch(r"[0-9A-Z]{6}", 코드):
                목록.append((코드, 이름[:20]))
    return 목록


def 업종멤버():
    """{코드: (업종, 이름, 시장 1/0, 시가총액)} — 네이버 업종별 종목 목록을 업종표대로 묶는다"""
    네이버 = {이름: 업 for 업, (_, 이름들) in 업종표.items() for 이름 in 이름들}
    try:
        그룹들 = json.loads(받기("https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=100",
                              "ind_list_1.json")).get("groups", [])
    except ValueError:
        그룹들 = []
    결과, 안묶임 = {}, []
    for g in 그룹들:
        업 = 네이버.get(g.get("name"))
        if not 업:
            if g.get("name") != "기타":
                안묶임.append(g.get("name"))
            continue
        for 쪽 in range(1, (int(g.get("totalCount", 0)) + 99) // 100 + 1):
            글 = 받기(f"https://m.stock.naver.com/api/stocks/industry/{g['no']}?page={쪽}&pageSize=100",
                     f"ind_{g['no']}_{쪽}.json")
            try:
                항목들 = json.loads(글).get("stocks", [])
            except ValueError:
                항목들 = []
            for s in 항목들:
                코드 = str(s.get("itemCode", ""))
                if s.get("stockEndType") != "stock" or s.get("sosok") not in ("0", "1") \
                        or not re.fullmatch(r"[0-9A-Z]{6}", 코드):
                    continue
                try:
                    시총 = int(str(s.get("marketValue", "0")).replace(",", ""))
                except ValueError:
                    시총 = 0
                결과[코드] = (업, str(s.get("stockName", ""))[:20], 1 if s["sosok"] == "0" else 0, 시총)
    if 안묶임:
        print(f"  업종표에 없는 네이버 업종(무시함): {', '.join(안묶임)}")
    return 결과


_분봉줄 = re.compile(r'data="(\d{8})(\d{4})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|')


def 분봉(코드):
    """{날짜: [(분, 종가)]}  분 = 09:00 부터 센 분"""
    글 = 받기(
        f"https://fchart.stock.naver.com/sise.nhn?symbol={코드}&timeframe=minute&count=5000&requestType=0",
        f"min_{코드}.xml", 인코딩="euc-kr",
    )
    날들 = {}
    for d, hm, c in _분봉줄.findall(글):
        분 = int(hm[:2]) * 60 + int(hm[2:]) - 540
        if 0 <= 분 <= 390 and float(c) > 0:
            날들.setdefault(d, []).append((분, float(c)))
    return 날들


def 평균(xs):
    return sum(xs) / len(xs) if xs else 0.0


파킨슨 = 1 / math.sqrt(4 * math.log(2))      # 고가/저가 폭 → 장중 표준편차


def 하루들(봉):
    """{날짜: (갭, 장중, 폭)} — 권리락·액면분할처럼 가격제한폭을 넘은 날은 뺀다"""
    결과 = {}
    for (d0, o0, h0, l0, c0), (d, o, h, l, c) in zip(봉, 봉[1:]):
        갭, 장중 = math.log(o / c0), math.log(c / o)
        if not (-0.37 < 갭 + 장중 < 0.275):
            continue
        결과[d] = (갭, 장중, math.log(h / l) * 파킨슨)
    return 결과


def 기초(코드, 이름, 시장키, 지수날들):
    """
    종목 하나의 베타와 '지수로 설명 안 되는 움직임'.
    {코드, 이름, 시장, 베타, 날: {날짜: [갭잔차, 장중잔차, 고유폭²]}, 수익: [하루 수익률]} 또는 None(자료 부족)
    """
    날 = 하루들(일봉(코드))
    날짜들 = [d for d in sorted(날) if d in 지수날들]
    if len(날짜들) < 최소일수:
        return None
    ri = [날[d][0] + 날[d][1] for d in 날짜들]
    rm = [지수날들[d][0] + 지수날들[d][1] for d in 날짜들]
    mi, mm = 평균(ri), 평균(rm)
    공분산 = sum((x - mi) * (y - mm) for x, y in zip(ri, rm))
    분산m = sum((y - mm) ** 2 for y in rm)
    베타 = max(0.2, min(2.2, 공분산 / 분산m))
    줄 = {}
    for d in 날짜들:
        (갭, 장중, 폭), (mg, mn, mp) = 날[d], 지수날들[d]
        줄[d] = [갭 - 베타 * mg, 장중 - 베타 * mn, max(0.0, 폭 * 폭 - (베타 * mp) ** 2)]
    return {"코드": 코드, "이름": 이름, "시장": 시장키, "베타": 베타, "날": 줄, "수익": ri}


_자름 = 0.15        # 업종 평균을 낼 때 한 종목의 하루 움직임은 여기까지만 친다 (상한가 하나가 업종을 끌지 않게)


def _자른(x):
    return max(-_자름, min(_자름, x))


def 업종분석(종목들, 업종별표본, 날짜들):
    """
    업종마다 날짜별 공통 움직임을 만들고, 종목마다 업종 민감도(감마)를 구해 잔차에서 업종 몫을 뺀다.
    종목들: {코드: 기초() 결과 + "업종"}.  돌려주는 값: (업종 정보 {이름: {...}}, 업종날 {이름: [[갭, 장중] × 날짜수]})
    """
    정보, 업종날 = {}, {}
    for 업, (설명, _) in 업종표.items():
        표본 = [종목들[c] for c in 업종별표본.get(업, [])]
        합 = {}                                   # 날짜 → [갭 합, 장중 합, 종목 수]
        for s in 표본:
            for d, (g, n, _) in s["날"].items():
                칸 = 합.setdefault(d, [0.0, 0.0, 0])
                칸[0] += _자른(g)
                칸[1] += _자른(n)
                칸[2] += 1
        움직임 = {d: (v[0] / v[2], v[1] / v[2]) for d, v in 합.items() if v[2] >= 업종_최소종목}
        표본코드 = {s["코드"] for s in 표본}
        잡음 = math.sqrt(평균([움직임[d][1] ** 2 for d in 날짜들 if d in 움직임]) or 1e-12)

        감마들, 몫들 = [], []
        for s in 종목들.values():
            if s.get("업종") != 업:
                continue
            xs, ys = [], []
            for d, (g, n, _) in s["날"].items():
                if d not in 움직임:
                    continue
                fg, fn = 움직임[d]
                if s["코드"] in 표본코드:          # 자기 자신은 빼고 본다 (안 빼면 민감도가 부풀려진다)
                    k = 합[d][2]
                    if k - 1 < 업종_최소종목:
                        continue
                    fg = (fg * k - _자른(g)) / (k - 1)
                    fn = (fn * k - _자른(n)) / (k - 1)
                xs.append(fg + fn)
                ys.append(g + n)
            if len(xs) < 최소일수 // 2:
                s["감마"] = 0.0
                continue
            mx, my = 평균(xs), 평균(ys)
            분산x = sum((x - mx) ** 2 for x in xs)
            감마 = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / 분산x if 분산x > 0 else 0.0
            s["감마"] = 감마 = max(0.0, min(1.8, 감마))
            for d, 값 in s["날"].items():          # 업종 몫을 뺀다
                if d in 움직임:
                    fg, fn = 움직임[d]
                    값[0] -= 감마 * fg
                    값[1] -= 감마 * fn
                    값[2] = max(0.0, 값[2] - (감마 * 잡음) ** 2)
            if s["코드"] in 표본코드:
                감마들.append(감마)
                분산r = sum((r - 평균(s["수익"])) ** 2 for r in s["수익"]) / len(s["수익"])
                몫들.append(감마 ** 2 * (분산x / len(xs)) / 분산r if 분산r > 0 else 0.0)

        줄 = [list(움직임.get(d, (0.0, 0.0))) for d in 날짜들]
        합계 = [g + n for g, n in 줄]
        정보[업] = {
            "설명": 설명,
            "수": len(표본),
            "추세": round(평균(합계), 6),          # 3년 동안 이 업종이 지수보다 하루 평균 얼마나 더 올랐는지
            "변동": round(math.sqrt(평균([x * x for x in 합계])), 5),
            "잡음": round(잡음, 5),
            "감마": round(평균(감마들), 3),
            "몫": round(평균(몫들), 3),            # 종목 등락 가운데 업종으로 설명되는 비중
        }
        업종날[업] = [[round(g, 5), round(n, 5)] for g, n in 줄]
        print(f"  {업:<4} 표본 {len(표본):>2}종목 · 하루 변동 {정보[업]['변동'] * 100:.2f}% · "
              f"민감도 {정보[업]['감마']:.2f} · 업종 몫 {정보[업]['몫'] * 100:.0f}% · "
              f"3년 누적 {(math.exp(sum(합계)) - 1) * 100:+.0f}%")
    return 정보, 업종날


def 마무리(시장, 종목들):
    """한 시장의 종목들 → (종목 목록, 잔차 표본, garch 계수, 통계)"""
    행들, 자료, 전체수익 = [], [], []          # 자료: 종목마다 [(갭잔차, 장중잔차, 고유폭²)]
    for s in 종목들:
        줄 = [tuple(s["날"][d]) for d in sorted(s["날"])]
        변동 = math.sqrt(평균([(a + b) ** 2 for a, b, _ in 줄]))
        행들.append([s["코드"], s["이름"], round(s["베타"], 3), round(변동, 5),
                    s.get("업종"), round(s.get("감마", 0.0), 3)])
        자료.append(줄)
        전체수익 += s["수익"]

    # --- GARCH(1,1): 어제의 움직임이 오늘 변동성에 얼마나 남는지 (모든 종목을 묶어서 추정) ---
    def 손실(a, 지속):
        b, 합 = 지속 - a, 0.0
        for 줄, 행 in zip(자료, 행들):
            긴 = 행[3] * 행[3]
            s2 = 긴
            for eg, en, _ in 줄:
                e2 = (eg + en) ** 2
                합 += math.log(s2) + e2 / s2
                s2 = 긴 * (1 - 지속) + a * e2 + b * s2
        return 합

    최선 = None
    for a in (0.02, 0.04, 0.06, 0.08, 0.10, 0.13, 0.16):
        for 지속 in (0.80, 0.88, 0.93, 0.96, 0.98, 0.99):
            if a >= 지속:
                continue
            값 = 손실(a, 지속)
            if 최선 is None or 값 < 최선[0]:
                최선 = (값, a, 지속 - a)
    _, a, b = 최선
    print(f"  garch: 알파 {a}, 베타 {b}")

    # --- 표준화 잔차 (갭, 장중, 폭) ---
    표본 = []
    for 줄, 행 in zip(자료, 행들):
        긴 = 행[3] * 행[3]
        s2 = 긴
        for eg, en, 폭2 in 줄:
            s = math.sqrt(s2)
            표본.append((eg / s, en / s, math.sqrt(폭2) / s))
            s2 = 긴 * (1 - a - b) + a * (eg + en) ** 2 + b * s2
    # 살아남은 대형주만 모아서 생기는 '고유 상승 편향'은 뺀다 (시장 전체의 오름세는 지수 쪽에 들어 있다)
    mg, mn = 평균([z[0] for z in 표본]), 평균([z[1] for z in 표본])
    표본 = [(g - mg, n - mn, p) for g, n, p in 표본]
    random.Random(7).shuffle(표본)
    표본 = 표본[:표본수]

    m = 평균(전체수익)
    분산 = 평균([(x - m) ** 2 for x in 전체수익])
    통계 = {
        "종목수": len(행들),
        "일변동성_평균": round(math.sqrt(분산), 5),
        "첨도": round(평균([(x - m) ** 4 for x in 전체수익]) / 분산 ** 2, 2),
        "하루5%이상": round(sum(abs(x) > 0.0488 for x in 전체수익) / len(전체수익), 4),
        "하루15%이상": round(sum(abs(x) > 0.14 for x in 전체수익) / len(전체수익), 5),
    }
    print(f"  통계: {통계}")
    return 행들, [[round(v, 3) for v in z] for z in 표본], [a, b], 통계


def 장중모양(코드들, 칸수=26):
    """하루 변동이 장중 어느 시간대에 몰리는지 (칸마다의 분산 비중, 합 1)"""
    합, 날수 = [0.0] * 칸수, 0
    for 코드 in 코드들:
        for d, 점 in 분봉(코드).items():
            if len(점) < 300:
                continue
            점.sort()
            칸 = [0.0] * 칸수
            for (m0, c0), (m1, c1) in zip(점, 점[1:]):
                칸[min(칸수 - 1, m1 * 칸수 // 391)] += math.log(c1 / c0) ** 2
            전체 = sum(칸)
            if 전체 > 0:
                날수 += 1
                for i in range(칸수):
                    합[i] += 칸[i] / 전체
    if 날수 < 20:
        return None, 날수
    return [round(v / 날수, 4) for v in 합], 날수


def main():
    os.makedirs(원본폴더, exist_ok=True)
    print(f"기간 {시작날} ~ {끝날}, 원본 폴더 {원본폴더}")

    코스피, 코스닥 = 하루들(일봉("KOSPI")), 하루들(일봉("KOSDAQ"))
    날짜들 = sorted(set(코스피) & set(코스닥))
    if len(날짜들) < 500:
        sys.exit(f"지수 자료가 너무 적습니다 ({len(날짜들)}일)")
    지수 = [[d] + [round(v, 5) for v in 코스피[d] + 코스닥[d]] for d in 날짜들]
    for 이름, 날 in (("코스피", 코스피), ("코스닥", 코스닥)):
        r = [날[d][0] + 날[d][1] for d in 날짜들]
        print(f"{이름} 지수: {len(r)}일, 하루 변동성 {math.sqrt(평균([x * x for x in r])) * 100:.2f}%, "
              f"3년 누적 {(math.exp(sum(r)) - 1) * 100:+.0f}%")
    지수날 = {1: 코스피, 0: 코스닥}

    print("업종 목록 받는 중…")
    멤버 = 업종멤버()
    if len(멤버) < 500:
        sys.exit(f"업종 목록을 못 받았습니다 ({len(멤버)}종목)")

    종목들, 순서 = {}, {1: [], 0: []}          # 코드 → 기초 결과 / 시장 → 코드 (시가총액 순)

    def 넣기(코드, 이름, 시장키):
        if 코드 in 종목들:
            return True
        s = 기초(코드, 이름, 시장키, 지수날[시장키])
        if s is None:
            return False
        s["업종"] = 멤버[코드][0] if 코드 in 멤버 else None
        종목들[코드] = s
        순서[시장키].append(코드)
        return True

    for 키, 시장 in ((1, "KOSPI"), (0, "KOSDAQ")):
        print(f"[{시장}] 시가총액 상위 종목 받는 중…")
        n = 0
        for 코드, 이름 in 상위종목(시장):
            if n >= 종목수:
                break
            n += 넣기(코드, 이름, 키)

    print("업종별 상위 종목 받는 중…")
    업종별 = {}
    for 코드, (업, 이름, 시장키, 시총) in 멤버.items():
        # 보통주만 (우선주는 본주를 그대로 따라가서 업종 움직임을 부풀린다). 스팩은 주가가 거의 안 움직여서 뺀다
        if 코드.endswith("0") and "스팩" not in 이름:
            업종별.setdefault(업, []).append((시총, 코드, 이름, 시장키))
    업종별표본 = {}
    for 업 in 업종표:
        고른 = []
        for 시총, 코드, 이름, 시장키 in sorted(업종별.get(업, []), reverse=True):
            if len(고른) >= 업종표본수:
                break
            if 넣기(코드, 이름, 시장키):
                고른.append(코드)
        업종별표본[업] = 고른
    # 시장마다 업종별 종목을 채운다. 안 채우면 코스닥 식품주가 코스피 대형 식품주의 성격을 물려받는다
    for 업 in 업종표:
        for 키 in (1, 0):
            있는 = sum(1 for c in 순서[키] if 종목들[c]["업종"] == 업)
            for 시총, 코드, 이름, 시장키 in sorted(업종별.get(업, []), reverse=True):
                if 있는 >= 업종시장_종목수:
                    break
                if 시장키 == 키 and 코드 not in 종목들:
                    있는 += 넣기(코드, 이름, 키)
    print(f"  분석 종목 코스피 {len(순서[1])}개, 코스닥 {len(순서[0])}개")

    print("업종 분석…")
    업종정보, 업종날 = 업종분석(종목들, 업종별표본, 날짜들)

    결과 = {"만든날": f"{끝날}", "기간": [날짜들[0], 날짜들[-1]], "지수": 지수,
            "업종": 업종정보, "업종날": 업종날,
            "종목": {}, "잔차": {}, "garch": {}, "통계": {}}
    분봉대상 = []
    for 키, 시장 in ((1, "KOSPI"), (0, "KOSDAQ")):
        print(f"[{시장}] 잔차·변동성 분석…")
        행들, 표본, 계수, 통계 = 마무리(시장, [종목들[c] for c in 순서[키]])
        결과["종목"][str(키)] = 행들
        결과["잔차"][str(키)] = 표본
        결과["garch"][str(키)] = 계수
        결과["통계"][str(키)] = 통계
        분봉대상 += 순서[키][:분봉종목수]

    print("분봉 받는 중…")
    모양, 날수 = 장중모양(분봉대상)
    if 모양 is None:
        print(f"  분봉이 모자라서({날수}일) 장중 모양은 기본값을 씁니다")
    else:
        print(f"  {날수}종목·일 기준: {모양}")
    결과["장중모양"] = 모양

    with open(결과파일, "w", encoding="utf-8") as f:
        json.dump(결과, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료: {결과파일} ({os.path.getsize(결과파일) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
