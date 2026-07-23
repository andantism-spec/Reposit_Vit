# PC Remote Control (WebRTC)

브라우저만으로 **인터넷 어디서나** 이 PC의 화면을 실시간으로 보고 마우스·키보드를 조작할 수 있는
원격 데스크톱 도구입니다. 영상은 WebRTC로 P2P 저지연 전송되고, NAT 통과(STUN/TURN)를 지원해
서로 다른 네트워크 사이에서도 연결됩니다. 별도 클라이언트 설치가 필요 없습니다.

- 화면: WebRTC 비디오 트랙 (H.264/VP8, MJPEG보다 저지연·저대역폭)
- 입력: WebRTC DataChannel → `pyautogui` (마우스 이동/클릭/드래그/스크롤, 텍스트, 단축키)
- 접속 보호: 토큰 인증
- NAT 통과: STUN 기본 + TURN 릴레이 설정 가능

## 구조

```
[원격지 브라우저] ──(1) HTTP /offer, /config (시그널링)──► [server.py (이 PC)]
        │                                                        │
        └────────(2) WebRTC P2P: 영상 + 입력 (STUN/TURN)◄────────┘
```

- **(1) 시그널링**은 평문 HTTP입니다. PC가 NAT 뒤에 있으면 이 포트를 외부에서 접근할 수 있어야
  하므로 터널(cloudflared/ngrok)이나 포트 포워딩이 필요합니다.
- **(2) 영상·입력**은 WebRTC로 P2P 전송되며, 직접 연결이 안 되는 네트워크에서는 TURN 릴레이를
  거칩니다. 이 트래픽은 시그널링 터널을 통하지 않습니다.

## 설치

```bash
pip install -r requirements.txt
```

플랫폼별 사전 준비:

- **Windows**: 추가 설정 없음.
- **macOS**: `시스템 설정 → 개인정보 보호 및 보안`에서 터미널(또는 Python)에 **화면 기록**과
  **손쉬운 사용(제어 허용)** 권한 부여.
- **Linux**: X11 세션 권장. Wayland에서는 화면 캡처/입력이 제한될 수 있습니다.

> `aiortc`는 내부적으로 PyAV(FFmpeg)를 사용합니다. 대부분 pip 휠로 자동 설치되지만, 빌드가
> 필요한 환경에서는 FFmpeg 개발 라이브러리가 있어야 할 수 있습니다.

## 실행 (제어 대상 PC에서)

```bash
# 토큰 직접 지정
python server.py --host 0.0.0.0 --port 8000 --token my-secret

# 토큰 생략 시 무작위 토큰이 생성되어 콘솔에 출력됩니다
python server.py
```

## 인터넷에서 접속하기

PC가 공유기/NAT 뒤에 있으면 시그널링 포트(기본 8000)를 외부에 노출해야 합니다. 가장 쉬운 방법:

**A. Cloudflare Tunnel (계정 없이 즉석 사용)**
```bash
cloudflared tunnel --url http://localhost:8000
# 출력되는 https://xxxx.trycloudflare.com 주소로 원격지에서 접속
```

**B. ngrok**
```bash
ngrok http 8000
```

**C. 포트 포워딩**: 공유기에서 외부 포트 → PC의 8000으로 포워딩 후 `http://<공인IP>:8000/` 접속.

터널이 HTTPS를 제공하면 브라우저의 보안 컨텍스트 문제도 함께 해결됩니다(WebRTC 권장).

> **한 번에 띄우기**: 원격제어 서버 + cloudflared + coturn을 함께 올리는 Docker Compose 예시는
> [`deploy/full-stack/`](deploy/full-stack/)에 있습니다 (리눅스 X11 데스크톱 전제).

### 직접 연결이 안 될 때 (TURN)

양쪽이 까다로운 NAT(대칭형) 뒤에 있으면 STUN만으로는 P2P가 안 되어 **TURN 릴레이**가 필요합니다.
직접 운영하는 TURN(coturn 등)이나 관리형 TURN 자격증명을 지정하세요.

```bash
python server.py \
  --turn-url turn:your-turn-host:3478 \
  --turn-user USERNAME \
  --turn-pass PASSWORD
```

환경변수 `TURN_URL`, `TURN_USER`, `TURN_PASS`로도 지정할 수 있습니다.

TURN 서버를 직접 운영하려면 **[coturn 셋업 가이드](docs/coturn-setup.md)**를 참고하세요
(설치·설정·인증·TLS·검증까지 정리되어 있습니다).

## 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--host` | `0.0.0.0` | 바인드 주소 |
| `--port` | `8000` | 포트 |
| `--token` | 무작위 | 접속 토큰 (환경변수 `REMOTE_TOKEN`) |
| `--max-width` | `0` | 프레임 최대 가로 폭(px). 0이면 원본 해상도 |
| `--stun` | Google STUN | STUN URL. `none`으로 비활성화 |
| `--turn-url` / `--turn-user` / `--turn-pass` | 없음 | TURN 릴레이 설정 |

## 사용법 (웹 UI)

- 화면을 **클릭/터치**하면 클릭, **드래그**하면 드래그.
- **우클릭: ON** 버튼을 누르면 다음 클릭이 오른쪽 버튼으로 동작.
- 상단 입력창에 글자를 입력하면 PC로 전송, **Enter**로 엔터키.
- `Ctrl+C`·`Ctrl+V`·`Esc`·`⌫` 단축키 버튼 제공.
- 마우스 휠 스크롤은 데스크톱 브라우저에서 지원.

## 보안 주의사항

- 이 도구는 **PC의 완전한 제어 권한**을 노출합니다. 강력한 토큰을 쓰고, 사용하지 않을 때는
  서버를 종료하세요.
- 시그널링은 평문 HTTP이므로 인터넷 노출 시 **HTTPS 터널**(cloudflared/ngrok) 뒤에서 사용하세요.
- WebRTC 미디어/데이터 채널 자체는 DTLS/SRTP로 암호화됩니다.
- 공용 STUN은 IP 발견에만 쓰이며 미디어를 중계하지 않습니다. TURN을 쓸 때는 신뢰할 수 있는
  서버를 사용하세요.
