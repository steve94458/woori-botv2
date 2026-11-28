"""스탠드 요청 처리 (관리자가 클로드 코드로 쓴다)

    venv/bin/python 스탠드요청.py                       대기 중인 요청 보기
    venv/bin/python 스탠드요청.py 완성 [번호] [능력 JSON 또는 .json 파일]
    venv/bin/python 스탠드요청.py 거절 [번호] [이유]      (생성 요청이면 디스크, 변경 요청이면 골드를 돌려준다)
변경 요청 완성: 능력 dict 면 '[#N …]' 능력을 바꾸고, list 면 능력 전체를 그 목록으로 (전체 변경 요청)

능력 JSON 형식은 RPG.py '스탠드' 칸 주석 참고. 처리하면 요청한 사람에게 봇 DM 을 보낸다.
봇이 켜져 있어도 된다 (같은 bot.db, 요청 한 줄만 바꾼다). 진행 중인 전투엔 다음 던전부터 들어간다.
"""
import json
import os
import re
import sys
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))
import RPG  # noqa: E402


def _토큰():
    for 줄 in open(".env", encoding="utf-8"):
        m = re.match(r"\s*DISCORD_TOKEN\s*=\s*(.+)", 줄)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def DM(user_id, 글):
    토큰 = _토큰()
    if not 토큰:
        return False

    def 요청(경로, 몸):
        r = urllib.request.Request("https://discord.com/api/v10" + 경로, data=json.dumps(몸).encode(), method="POST",
                                   headers={"Authorization": "Bot " + 토큰, "Content-Type": "application/json",
                                            "User-Agent": "DiscordBot (local, 1)"})
        return json.load(urllib.request.urlopen(r, timeout=15))
    try:
        채널 = 요청("/users/@me/channels", {"recipient_id": str(user_id)})["id"]
        요청(f"/channels/{채널}/messages", {"content": 글[:2000]})
        return True
    except Exception as e:
        print("DM 실패:", e)
        return False


def 목록():
    대기 = RPG.스탠드요청목록()
    if not 대기:
        print("대기 중인 요청 없음")
    for r in 대기:
        c = RPG.캐릭터(r["user_id"]) or {}
        st = (RPG.스탠드(r["스탠드id"]) if r["스탠드id"] else None) if r["종류"] != "생성" else None
        if r["종류"] != "생성" and st is None:
            st = RPG.내스탠드(r["user_id"])
        가진 = RPG.내스탠드들(r["user_id"])
        print(f"#{r['id']} [{r['종류']}] {c.get('name')} ({r['user_id']}) 최고층 {c.get('best')} · 스탠드 '{r['이름']}'"
              + (f" Lv.{st['레벨']} 능력 {len(st['능력들'])}개" if st else "")
              + (f" · 가진 스탠드 {len(가진)}개: " + ", ".join(f"{x['이름']} Lv.{x['레벨']}" for x in 가진) if 가진 else ""))
        print(f"   요청: {r['내용']}")
        if st:
            for a in st["능력들"]:
                print(f"   기존: {json.dumps(a, ensure_ascii=False)}")


def main():
    a = sys.argv[1:]
    if not a:
        return 목록()
    if a[0] == "완성" and len(a) >= 3:
        원 = " ".join(a[2:])
        능력 = json.load(open(원, encoding="utf-8")) if 원.endswith(".json") and os.path.exists(원) else json.loads(원)
        ok, 글, r = RPG.스탠드처리(int(a[1]), 능력=능력)
        print(글)
        if ok:
            st = (RPG.스탠드(r["스탠드id"]) if r.get("스탠드id") else None) or RPG.내스탠드(r["user_id"])
            if r["종류"] == "변경":
                바뀜 = 능력 if isinstance(능력, list) else [능력]
                DM(r["user_id"], f"👤 스탠드 **{st['이름']}** 능력이 바뀌었어요! (요청 #{r['id']})\n"
                   + "\n".join(f"「{a['이름']}」 {a.get('설명', '')}" for a in 바뀜)[:1700]
                   + "\n다음 던전부터 들어가요. `!RPG 스탠드` 로 확인!")
                return
            DM(r["user_id"], f"👤 스탠드 **{st['이름']}**" + (" 완성!" if r["종류"] == "생성" else "에 새 능력이 생겼어요!")
               + f"\n「{능력['이름']}」 {능력.get('설명', '')}\n`!RPG 스탠드` 로 확인 · "
               + ("`!RPG 펫 장착 스탠드` 로 데리고 다녀요." if r["종류"] != "생성" or r.get("처음")
                  else f"`!RPG 스탠드 바꾸기 {next((x['번호'] for x in RPG.내스탠드들(r['user_id']) if x['id'] == st['id']), '')}` 로 끼면 데리고 다녀요."))
        return
    if a[0] == "거절" and len(a) >= 3:
        ok, 글, r = RPG.스탠드처리(int(a[1]), 거절=" ".join(a[2:]))
        print(글)
        if ok:
            DM(r["user_id"], f"👤 스탠드 요청 #{r['id']}이 반려됐어요: {' '.join(a[2:])}"
               + ("\n💿 디스크는 돌려드렸어요. 다시 요청해 주세요." if r["종류"] == "생성"
                  else "\n💰 골드는 돌려드렸어요." if r["종류"] == "변경" else ""))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
