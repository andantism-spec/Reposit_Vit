# 전체 스택 Docker Compose

원격제어 서버 · cloudflared(공개 URL) · coturn(선택)을 한 번에 띄우는 예시입니다.

```
deploy/full-stack/
├── Dockerfile           # 원격제어 서버 이미지 (X11/aiortc 런타임 포함)
├── docker-compose.yml   # remote + cloudflared + coturn(profile)
└── .env.example         # REMOTE_TOKEN 등
```

## ⚠️ 전제 조건

이 구성은 **제어 대상 PC가 리눅스 X11 데스크톱**일 때만 동작합니다. 컨테이너가 호스트의
화면(X11 소켓)과 입력 장치에 접근해야 하기 때문입니다.

- **Wayland 세션**: 화면 캡처/입력이 막힙니다 → X11 세션으로 로그인하거나 호스트에서 직접 실행.
- **macOS / Windows**: 컨테이너로 호스트 화면 제어가 불가 → `python server.py`를 호스트에서 직접 실행하고,
  cloudflared/coturn만 필요에 따라 사용하세요.

## 실행

```bash
cd deploy/full-stack

# 1) 환경변수 설정
cp .env.example .env
#    .env 에서 REMOTE_TOKEN을 강한 값으로 설정

# 2) 컨테이너가 X 서버에 접근하도록 허용 (호스트에서 1회)
xhost +local:

# 3) 기동 (이미지 빌드 포함)
docker compose up -d --build

# 4) 공개 접속 URL 확인
docker compose logs -f cloudflared
#    로그의 https://xxxx.trycloudflare.com 주소로 원격지에서 접속 → 토큰 입력
```

중지:

```bash
docker compose down
xhost -local:      # 접근 허용 원복(권장)
```

## coturn까지 함께 띄우기 (선택)

대칭형 NAT 등으로 P2P가 안 될 때 TURN 릴레이가 필요합니다. `turn` 프로필로 함께 기동합니다.

```bash
# ../coturn/turnserver.conf 의 <PUBLIC_IP>/<REALM>/<TURN_USER>/<TURN_PASS> 를 먼저 수정
# .env 의 TURN_URL/TURN_USER/TURN_PASS 도 동일 값으로 채움
docker compose --profile turn up -d --build
```

> ⚠️ TURN 릴레이는 **공인 IP**가 있어야 제대로 동작합니다. 제어 대상 PC가 NAT 뒤에 있다면
> coturn은 이 호스트가 아니라 **별도의 공인 IP 서버(VPS)** 에 두는 것이 맞습니다
> (그 경우 [`deploy/coturn/`](../coturn/)의 단독 compose 사용). 여기 `turn` 프로필은 호스트가
> 공인 IP를 가진 경우를 위한 편의 구성입니다.

## 방화벽 포트

- 시그널링만 cloudflared로 노출하면 서버 포트(8000)를 외부에 직접 열 필요는 없습니다.
- TURN을 이 호스트에서 운영한다면 `3478/udp`, `3478/tcp`, (TLS) `5349`, `49152-65535/udp` 개방이 필요합니다.

## 문제 해결

| 증상 | 확인 |
|------|------|
| `cannot open display` / 검은 화면 | `xhost +local:` 실행 여부, `.env`의 `DISPLAY` 값, X11 세션(Wayland 아님) |
| 입력이 안 먹힘 | `libxtst6`(XTEST) 포함된 이미지인지(Dockerfile 확인), X11 세션 여부 |
| cloudflared URL 안 보임 | `docker compose logs cloudflared` 에서 `trycloudflare.com` 검색 |
| TURN relay 후보 없음 | 호스트 공인 IP, `turnserver.conf`의 `external-ip`, 방화벽 포트 |

상세한 coturn 설정·검증은 [coturn 셋업 가이드](../../docs/coturn-setup.md)를 참고하세요.
