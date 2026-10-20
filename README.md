# VirtualMonitor

탭 2개짜리 Windows 프로그램 (`app.py`) + 원격 PC용 호스트 (`host.py`).

| 탭 | 기능 |
|---|---|
| 가상 디스플레이 | 만들기 / 삭제 (Windows 우클릭 > 디스플레이 설정에 실제 모니터로 나타남) |
| 원격 | IP 입력 → 접속 → 디스플레이 번호 선택 → 원격 접속하기 (원본 해상도·디스플레이 주사율에 맞춘 fps(최대 144), 전체화면) |

## 설치
```
pip install -r requirements.txt
```

## 사용
1. **원격 PC(보여줄 PC)**: `python host.py` → 콘솔에 IP 와 6자리 비밀번호가 표시됨
   - 처음 실행 시 Windows 방화벽 허용 팝업에서 **개인 네트워크 허용** (TCP 50505, UDP 50506)
2. **내 PC**: `python app.py` → **원격** 탭 → IP/비밀번호 입력(또는 `PC 검색`) → `접속` → 디스플레이 선택 → `원격 접속하기`
   - 보기 창: `F11`/더블클릭 = 전체화면 전환, `Esc` = 전체화면 해제, 창 닫기 = 종료

## 가상 디스플레이 탭
Windows 에 모니터를 추가하려면 Indirect Display Driver 가 필요해서, 서명된 오픈소스
[Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver) 를 사용합니다.
- 드라이버가 없으면 탭에 `드라이버 설치 (winget)` 버튼이 나타남 (`winget install --id=VirtualDrivers.Virtual-Display-Driver -e`)
- 만들기/삭제는 **관리자 권한**이 필요 (버튼을 누르면 관리자로 다시 실행할지 물어봄)
- 만들기 = `C:\VirtualDisplayDriver\vdd_settings.xml` 의 모니터 수 +1 후 드라이버 장치 재시작
- 삭제 = 모니터 수 -1. 마지막 1개는 드라이버가 최소 1개를 요구해서 장치를 꺼서 0개로 만듦
- 가상 디스플레이를 만든 PC 에서 `host.py` 를 실행하면 그 모니터도 원격 목록에 같이 나옴

## 보안 (꼭 읽기)
- 비밀번호는 HMAC 챌린지로 확인하고 네트워크로 전송하지 않지만, **화면 데이터는 암호화되지 않음** → 신뢰하는 가정/사내 Wi-Fi 에서만 사용
- 보기 전용입니다 (마우스/키보드 입력 전달 없음)
- 본인이 관리하는 PC 에서만 `host.py` 를 실행하세요. 종료는 `Ctrl+C`

## 구조
`protocol.py` 프로토콜 · `host.py` 호스트 · `capture.py` DXGI(GPU)/mss 캡처 · `remote_client.py` 뷰어 네트워크 · `virtual_display.py` 가상 디스플레이 · `app.py` GUI
