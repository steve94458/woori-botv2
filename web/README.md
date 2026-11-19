# 우리거래소 — 우리봇v2 웹사이트

디스코드 계정으로 로그인해서 주식·코인을 사고팔고, 채굴장을 관리하는 웹사이트.
봇과 **같은 bot.db 와 tools.py 를 그대로 쓰기 때문에** 웹에서 한 거래와 디스코드에서 한 거래가 완전히 같은 규칙을 따른다.

```
디코 봇/
 ├─ 우리봇v2.py
 ├─ tools.py        ← 새 버전으로 교체
 ├─ bot.db
 └─ web/            ← 이 폴더를 통째로 넣는다
     ├─ server.py
     ├─ static/index.html
     ├─ requirements.txt
     └─ .env        ← .env.example 을 복사해서 만든다
```

## 1. 디스코드 개발자 포털 설정

1. https://discord.com/developers/applications 에서 **봇이 들어 있는 그 앱**을 연다
2. 왼쪽 **OAuth2** 탭
3. **Client ID** 를 복사한다
4. **Client Secret** 에서 Reset Secret 을 눌러 새로 만들고 복사한다 (한 번만 보여준다)
5. **Redirects** 에 `Add Redirect` → 아래 주소를 넣고 저장
   - 집 안에서만 쓸 때: `http://라즈베리파이IP:8000/callback`
   - 밖에서도 쓸 때: 아래 4번에서 만든 주소 + `/callback`

## 2. 설정 파일

```bash
cd ~/Desktop/"디코 봇"/web
cp .env.example .env
nano .env
```

`SESSION_SECRET` 은 이 명령으로 만든다.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## 3. 설치와 실행

봇이 쓰는 venv 를 그대로 쓴다.

```bash
cd ~/Desktop/"디코 봇"
source venv/bin/activate
pip install -r web/requirements.txt

cd web
uvicorn server:app --host 0.0.0.0 --port 8000
```

같은 와이파이에서 `http://라즈베리파이IP:8000` 으로 들어가 본다.

PM2 로 계속 켜두려면:

```bash
cd ~/Desktop/"디코 봇"/web
pm2 start "../venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000" --name web
pm2 save
```

## 4. 밖에서도 접속하게 하기 (선택)

**Cloudflare Tunnel 을 권장한다.** 공유기 포트를 열지 않아도 되고 자동으로 https 가 된다.

```bash
cloudflared tunnel --url http://localhost:8000
```

나온 `https://....trycloudflare.com` 주소를

- `.env` 의 `DISCORD_REDIRECT_URI` 에 `/callback` 을 붙여 넣고
- 개발자 포털 Redirects 에도 똑같이 추가하고
- `.env` 의 `HTTPS_ONLY=1` 로 바꾼 뒤 웹 서버를 재시작한다.

(임시 주소는 재시작할 때마다 바뀐다. 고정 주소가 필요하면 Cloudflare 계정으로 이름 있는 터널을 만든다.)

## 4-1. 내 도메인의 /stock 으로 연결하기 (nginx + Let's Encrypt)

이미 https 도메인이 있으면 Cloudflare Tunnel 대신 이 방법을 쓴다.

**① `.env` 수정**

```
DISCORD_REDIRECT_URI=https://내도메인/stock/callback
HTTPS_ONLY=1
BASE_PATH=/stock
```

**② 디스코드 개발자 포털** → OAuth2 → Redirects 에 `https://내도메인/stock/callback` 추가

**③ 거래소 서버는 라즈베리파이 안에서만 열리게 실행**

nginx 가 대신 받아서 넘겨주므로 밖에 직접 열 필요가 없다.

```bash
cd ~/Desktop/"디코 봇"/web
pm2 delete web 2>/dev/null
pm2 start "../venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1" --name web
pm2 save
```

**④ nginx 설정**

`nginx_stock.conf` 안의 두 `location` 을 도메인 설정 파일의 `listen 443 ssl` 이 있는 `server { }` 블록 안에 붙여 넣는다.

```bash
sudo nano /etc/nginx/sites-available/default     # 도메인 설정 파일
sudo nginx -t                                    # 문법 검사. "successful" 이 나와야 한다
sudo systemctl reload nginx
```

`https://내도메인/stock` 으로 들어가 본다.

| 증상 | 원인 |
|---|---|
| 502 Bad Gateway | 거래소 서버가 꺼져 있음. `pm2 logs web` 확인 |
| 화면은 뜨는데 숫자가 전부 `-` | `proxy_pass http://127.0.0.1:8000/;` 끝의 `/` 가 빠짐 |
| 로그인하고 오면 도메인 첫 화면으로 감 | `.env` 에 `BASE_PATH=/stock` 이 없음 |
| 로그인이 계속 풀림 | `HTTPS_ONLY=1` 인데 http 로 접속함 |

## 5. 봇 쪽 패치

`봇_가격기록패치.py` 를 보고 봇 파일에 몇 줄을 넣는다.
이걸 안 하면 차트에 거래가 있었던 순간만 찍히고, 가격이 저절로 움직이는 건 기록되지 않는다.

## 문제 해결

| 증상 | 원인 |
|---|---|
| 로그인 누르면 디스코드에서 "잘못된 redirect_uri" | `.env` 의 주소와 개발자 포털 Redirects 가 한 글자라도 다름 (끝의 `/` 포함) |
| 로그인 후 다시 로그인 화면 | `HTTPS_ONLY=1` 인데 http 로 접속함 |
| `database is locked` | 봇의 `sqlite3.connect` 에 `timeout=15` 를 안 넣음 (패치 1번) |
| 차트가 점 하나 | 가격 기록이 아직 안 쌓임. 봇 패치 후 몇 시간 기다리면 채워진다 |
| 코인 거래가 막힘 | 봇의 코인시세갱신 루프가 안 돌고 있음. `on_ready` 에 `.start()` 확인 |

## 보안

- **Client Secret 과 `.env` 는 절대 남에게 보여주지 않는다.** 유출되면 개발자 포털에서 바로 Reset 한다.
- 거래 요청은 로그인 세션 + 전용 헤더가 있어야만 받는다. 다른 사이트에서 몰래 거래를 보낼 수 없다.
- 같은 사람이 0.7초 안에 연달아 거래를 보내면 막는다.
