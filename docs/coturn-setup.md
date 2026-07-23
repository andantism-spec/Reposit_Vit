# coturn TURN 서버 셋업 가이드

양쪽(제어 PC·원격지)이 까다로운 NAT(대칭형 NAT, 일부 기업/모바일 네트워크) 뒤에 있으면
STUN만으로는 WebRTC P2P 연결이 실패합니다. 이때 **TURN 릴레이**가 미디어/데이터를 중계해
연결을 성사시킵니다. 여기서는 오픈소스 TURN 서버 **coturn**을 직접 운영하는 방법을 다룹니다.

> TURN 서버는 **공인 IP를 가진 서버**(예: 클라우드 VM)에 두어야 합니다. NAT 뒤에 두면
> 릴레이 역할을 못 합니다. 저렴한 VPS 한 대면 충분합니다.

## 1. 사전 준비

- 공인 IP를 가진 리눅스 서버 (Ubuntu/Debian 예시)
- 도메인(선택, TLS를 쓸 경우 권장): 예 `turn.example.com`
- 방화벽/보안그룹에서 아래 포트 개방
  - `3478/udp`, `3478/tcp` — TURN/STUN
  - `5349/udp`, `5349/tcp` — TURN over TLS (turns)
  - `49152-65535/udp` — 릴레이용 미디어 포트 범위

## 2. 설치

```bash
sudo apt update
sudo apt install -y coturn
```

`/etc/default/coturn`에서 데몬 활성화:

```bash
# 파일 안의 다음 줄 주석 해제
TURNSERVER_ENABLED=1
```

## 3. 설정 (`/etc/turnserver.conf`)

기존 파일을 백업하고 아래 내용으로 교체합니다. `EXTERNAL_IP`, `REALM`,
`STATIC_USER/PASS`(또는 인증 비밀키)를 자신의 값으로 바꾸세요.

```conf
# --- 네트워크 ---
listening-port=3478
tls-listening-port=5349

# 서버의 공인 IP. 클라우드에서 NAT(사설IP↔공인IP)면 아래 형태로:
#   external-ip=<공인IP>/<사설IP>
external-ip=203.0.113.10

# 릴레이 미디어 포트 범위 (방화벽에서 열어둔 범위와 일치)
min-port=49152
max-port=65535

# --- 인증 ---
# realm은 아무 문자열이나 가능하지만 보통 도메인 사용
realm=turn.example.com

# (A) 간단한 고정 계정 방식 — 소규모/개인용에 적합
lt-cred-mech
user=remoteuser:strongpassword

# (B) 시간제한 자격증명(REST) 방식을 쓸 경우 위 user 줄 대신:
# use-auth-secret
# static-auth-secret=<긴-랜덤-시크릿>

# --- 보안/운영 권장값 ---
# 사설망 릴레이 차단(SSRF 방지)
no-multicast-peers
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=169.254.0.0-169.254.255.255

# 취약한 예전 프로토콜 제한
no-tlsv1
no-tlsv1_1

# 로그
log-file=/var/log/turnserver.log
simple-log

# --- TLS(선택, turns: 사용 시) ---
# cert=/etc/letsencrypt/live/turn.example.com/fullchain.pem
# pkey=/etc/letsencrypt/live/turn.example.com/privkey.pem
```

### 인증 방식 선택

- **(A) 고정 계정(`user=...`)**: 설정이 가장 간단합니다. 개인용/소규모에 적합하지만,
  자격증명이 노출되면 누구나 릴레이를 쓸 수 있으므로 강한 비밀번호를 쓰고 필요 시 교체하세요.
- **(B) 시간제한 자격증명(`use-auth-secret`)**: 서버가 만료 시각이 포함된 임시
  username/password를 발급하는 방식으로, 자격증명 유출 피해를 줄입니다. 프로덕션 권장.
  발급 방법은 아래 §6 참고.

## 4. TLS 인증서 (선택, 권장)

`turns:`(TLS)를 쓰면 방화벽이 UDP를 막는 네트워크에서도 443/5349 TCP로 우회할 수 있어
연결 성공률이 올라갑니다.

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d turn.example.com
# 발급 후 turnserver.conf의 cert/pkey 줄 주석 해제
```

## 5. 실행

```bash
sudo systemctl enable coturn
sudo systemctl restart coturn
sudo systemctl status coturn --no-pager
```

## 6. 시간제한 자격증명 발급 (방식 B를 쓸 때)

`static-auth-secret`과 아래 규칙으로 username/password를 만듭니다. username은
`<만료 epoch초>:<임의 사용자명>`, password는 그 username을 시크릿으로 HMAC-SHA1 한 뒤
base64 인코딩한 값입니다.

```bash
# 1시간 뒤 만료되는 자격증명 생성 예시
SECRET="여기에-static-auth-secret"
USER="$(( $(date +%s) + 3600 )):remote"
PASS="$(echo -n "$USER" | openssl dgst -binary -sha1 -hmac "$SECRET" | openssl base64)"
echo "username: $USER"
echo "password: $PASS"
```

## 7. 이 프로젝트에 연결

발급/설정한 값으로 `server.py`를 실행합니다.

```bash
# 방식 A (고정 계정)
python server.py --token my-secret \
  --turn-url turn:turn.example.com:3478 \
  --turn-user remoteuser \
  --turn-pass strongpassword

# TLS(turns)를 쓸 경우
python server.py --token my-secret \
  --turn-url turns:turn.example.com:5349 \
  --turn-user remoteuser \
  --turn-pass strongpassword
```

환경변수로도 지정할 수 있습니다:

```bash
export TURN_URL=turn:turn.example.com:3478
export TURN_USER=remoteuser
export TURN_PASS=strongpassword
python server.py --token my-secret
```

서버는 이 값을 브라우저에도 전달하므로(`/config`), 양쪽 피어가 동일한 TURN을 사용해
릴레이 연결을 맺습니다.

## 8. 동작 확인

- **Trickle ICE 테스트 페이지**로 TURN 자격증명이 유효한지 먼저 확인하세요:
  <https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/>
  STUN/TURN URL과 자격증명을 입력하고 "Gather candidates"를 눌렀을 때
  `typ relay` 후보가 나오면 TURN이 정상 동작하는 것입니다.
- 실제 연결이 릴레이를 탔는지는 브라우저의 `chrome://webrtc-internals`에서
  선택된 candidate pair의 타입이 `relay`인지로 확인할 수 있습니다.
- 서버 로그: `sudo tail -f /var/log/turnserver.log`

## 9. 문제 해결

| 증상 | 확인할 것 |
|------|-----------|
| relay 후보가 안 생김 | 방화벽에서 3478(udp/tcp)과 `min-port~max-port` 범위 개방 여부 |
| 인증 실패(401) | `realm`/`user` 일치, 시간제한 방식이면 만료시각·시크릿 확인 |
| 클라우드에서 연결 안 됨 | `external-ip=<공인IP>/<사설IP>` 형식으로 사설↔공인 매핑 지정 |
| UDP 차단 네트워크 | `turns:`(TLS, 5349/443 TCP) 사용으로 우회 |
| 서버 시각 오차 | 시간제한 자격증명은 서버 시계에 민감 → NTP 동기화 |

## 참고: 관리형 TURN

직접 운영이 부담되면 관리형 TURN 서비스(예: Cloudflare Calls TURN, Twilio Network
Traversal Service, Metered 등)의 자격증명을 발급받아 `--turn-url/-user/-pass`에 그대로
넣어도 됩니다. 설정 방식은 동일합니다.
