# 국내 선물 callback 수집·정규화

2026-11-04 (Asia/Seoul) · [Issue #4](https://github.com/TheMostEvident/quant-trading-system/issues/4) · [Extra Issue 1 #33](https://github.com/TheMostEvident/quant-trading-system/issues/33)

키움 OpenAPI+의 시장 callback을 원래 문자열로 보존하고 계약 v0.2.0의 MarketSnapshot으로 변환한다. Windows bridge 실행 경로와 플랫폼 독립적인 정규화·품질 감시·저장·재생 경로를 구현했다. **Linux 합성/fake-port 시험 83개가 통과했다. 실제 Windows·OCX·로그인·국내 선물 수신은 #33의 미검증 작업이다.**

## 데이터 경로와 책임

| 구성 | 구현 |
| --- | --- |
| `market/bridge.py`, `adapters/kiwoom/collect.py` | GUI 스레드에서 이벤트 시각·필드 복사; 공식 로그인 창; mock server flag 확인; 좁은 시장 시세 port |
| `market/spool.py` | bounded item/byte 큐; 별도 thread의 JSONL append·flush·fsync; 포화/IO 오류의 독립 health 파일 |
| `market/feed.py` | raw 먼저 커밋; 정규화·격리·연결/상태/호가/품질 감시; profile/spec/상태 계보·journal/outbox 연결 |
| `market/cli.py` | 완성된 JSONL frame 읽기·부분 기록 대기·실시간 follow·watchdog·전달/ACK·manifest |
| `market/profile.py`, `config/kiwoom.*.json` | origin·최소 상품 목록·trading date·미검증 상태·시각/크기 한도 |

모두 같은 호스트의 로컬 파일 경로에서 시작한다. bridge는 DB를 열지 않으며 코어와 같은 Python·비트수를 요구하지 않는다. 코어는 Python ≥3.12 / 실제 SQLite ≥3.51.3을 요구한다. bridge는 PyQt5/QAxContainer와 설치 OCX에 맞는 별도 interpreter가 필요하며 정확한 버전/비트수 조합은 #33에서 확인한다. core 설치 자체에는 PyQt5가 필요 없다.

## 파서 범위

[키움 공식 guide 1.7](https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.7.pdf) §8.5/8.6의 선물시세·선물호가잔량을 참조했다. FID 20/10/15와 21, 41–45/51–55/61–65/71–75를 사용한다. 해당 표의 설치 버전과 실제 callback 필드 의미는 KOA Studio에서 다시 확인한다.

- 원래 callback 인자·선택한 FID의 문자열·producer epoch/counter·수신 UTC/monotonic을 JSONL에 보존한다. 이것은 복호화 전 wire packet이 아니라 **OCX가 노출한 callback 표현**이다.
- BOOK는 상위 5단계를 매번 독립적으로 읽는다. 양수 활성 수량·정수 CONTRACT·정렬·중복 가격·교차 호가·중간 빈 레벨을 검사한다. 수량 0인 마지막 레벨은 활성 book에서 제거한다. 원천은 유지한다.
- 지수선물용 `SIGNED_MAGNITUDE` 가격 표현만 지원한다. 부호/공백은 raw에 보존하고 정규화 값은 크기를 사용한다. 일반 해외선물·음수 가격 상품의 파서로 재사용하지 않는다.
- TRADE의 부호 있는 수량은 명시적인 magnitude로 변환하고 aggressor는 UNKNOWN으로 남긴다. source trade ID·native event ID·공급자 sequence는 null이다. HHMMSS만으로 UTC·정확한 거래일·일련번호를 만들지 않는다.
- 정상 반복 호가를 내용 hash로 제거하지 않는다. 같은 producer/epoch/counter의 **동일 frame 재전달**만 idempotent 처리한다. 같은 frame ID의 다른 raw는 별도 capture로 보존하고 격리한다.
- `장시작시간`/FID 215 입력 경로는 제공하지만 **그 코드가 선택한 선물 세션의 상태를 나타내는지는 미확인**이다. 실제 프로필은 status_codes={}·session_mapping_verified=false가 기본이다. 합성 예시의 3/8 매핑을 실피드의 근거로 사용하지 않는다.
- `SetRealReg`는 선택 종목과 FID 전체(215 포함)를 화면 9000에 등록한다. 사용자 Windows에서 이 요청은 0을 반환했지만, 추가로 호출한 빈 종목코드/FID 215 등록은 -105(함수입력값 오류)로 거절됐다. 기본 경로에서 그 추가 요청을 제거했다. **선택한 선물의 실제 수신·초기 TR 필요 여부·상태 callback 범위는 계속 #33 확인 대상**이다. 등록 반환 성공만으로 수신/동기화 성공을 선언하지 않는다. 실제 TR bootstrap이나 별도 상태 구독이 필요한 것으로 확인되면 설치된 KOA 명세에 따라 어댑터를 보완한다.
- PR #34 당시 features는 null이었다. #5 구현 이후 기본 파이프라인은 검증된 입력에 [microstructure-v1](microstructure-features.md)을 추가한다. 미확인 상품/거래 상태에서는 계속 null이다. 진입 필터는 #6, 주문/계좌 원장은 #7 이후의 범위다.

## 이용 가능성과 복구

최초 상태는 연결 미확인·거래 상태 UNKNOWN·호가 없음이다. 모의 연결 확인, 검증된 상품/세션 프로필, CONTINUOUS 상태, 현재 수신한 정상 full book이 **모든 선택 상품**에 있어야 run health가 정상으로 바뀐다. 이는 시세 입력의 gate이며 전체 주문 위험 검사나 주문 실행기가 아니다.

- disconnect/producer 변경은 연결·상태·book을 무효화한다. 재접속 뒤 상태와 새로운 book을 다시 요구한다. bridge의 자동 재로그인 루프는 없으며 수동 재시작은 새 spool·새 run을 사용한다.
- 로컬 counter 누락·역순·clock 역행·정규화 오류·잘못된 book은 raw와 진단/STATUS를 남기고 차단한다. 원천 sequence가 없으므로 **브로커/거래소 상류의 모든 누락을 검출하지 못한다**.
- 로컬 누락/잘못된 book 이후에는 새 정상 full snapshot들로 현재 상태를 다시 확인한다. UTC/monotonic/HHMMSS 역행은 clock fault로 유지하며 새 producer epoch·연결·상태·book 확인이 필요하다. 자정은 임의 날짜 보정 대신 새 실행/세션 확인으로 처리한다.
- source 수신→코어 처리 지연이 한도를 넘으면 STALE이다. book은 수신 시각 기준 만료되며 TRADE/STATUS는 이전 book을 갱신하지 않는다. 데이터가 없어도 follow watchdog이 만료·heartbeat 손실을 기록한다.
- run health는 모든 상품의 상태를 함께 확인한다. 한 상품의 정상 입력이 다른 상품의 오류를 해제하지 않는다. 복구 때 확인한 book/status refs와 복구 규칙을 journal에 남긴다.
- 큐 포화·fsync 실패는 success로 표시하지 않는다. core는 별도 health 파일 오류 또는 heartbeat timeout으로 차단한다. fsync 호출·프로세스 시험은 물리 전원 손실 보증이 아니다.
- 실패한 health sidecar는 follow와 offline import 모두에서 차단한다. offline은 원천을 계속 보존할 수 있지만 같은 실행에서 새 정상 book이 transport fault를 해제하지 않는다.
- core 프로세스 재시작은 기존 run에 조용히 append하지 않는다. 새 run에서 상태를 다시 수집한다. 과거 run과 raw는 유지한다.

## 합성 실행

저장소를 설치한 core 환경에서 실행한다. 경로는 로컬 디스크를 사용하고 manifest·수집 파일은 새 이름을 선택한다.

```bash
python -m unittest discover -s tests -v
python -m quant_system market-ingest --input tests/fixtures/kiwoom-callbacks.synthetic.jsonl --database data/market.db --run-id market-demo --config config/kiwoom.synthetic.json --simulate-clock --manifest data/market.manifest.json
python -m quant_system integrity data/market.db
python -m quant_system replay data/market.manifest.json --run-id market-replay
python tools/benchmark_market.py --events 200
```

fixture는 raw 9건 → MarketSnapshot 10건이다. 연결·상태·top-5 book·trade·disconnect·재접속·상태 재확인·새 book·stale을 포함한다. 마지막 heartbeat 시점에는 book이 만료되어 healthy/usable=false가 기대값이다. 정확한 사건 UTC와 공급자 sequence는 null이며 모든 시장 메시지는 SYNTHETIC이다.

`--simulate-clock`은 offline SYNTHETIC 입력에서만 허용한다. 일반 import는 새 정규화가 완료된 시각을 ready_at으로 기록한다. 과거 callback 파일을 읽으면서 원래 producer의 처리 완료 시각을 알고 있었다고 표시하지 않는다. 실제 수집은 `--follow`를 사용한다.

## Windows 실행 순서 (#33)

PR #41 이후 필수 callback 0건 진단과 남은 1~5 검증은 [Windows 현장 검증 절차](windows-field-validation.md)를 따른다. 초기 TR과 필터 전 type 집계, 고정 프로필, bridge/core 동시 실행, 원본/DB/manifest 및 지표 재계산 대조, 두 run의 중단/재시작 검사 명령을 제공한다.

1. Windows에서 core 환경의 실제 Python/SQLite 버전을 확인하고 전체 테스트를 실행한다. 원래 45개 공통 기반 시험을 Windows에서 통과했는지 별도로 기록한다.

```powershell
python -c "import sys,sqlite3; print(sys.version); print(sqlite3.sqlite_version)"
python -m venv .venv-core
.\.venv-core\Scripts\python.exe -m pip install -e .
.\.venv-core\Scripts\python.exe -m quant_system environment --probe-kiwoom
.\.venv-core\Scripts\python.exe -m unittest discover -s tests -v
```

먼저 core의 최소 버전 조건을 충족하는 interpreter를 선택한다. Python 버전이 높아도 내장 SQLite가 조건을 만족하지 않을 수 있다. 이 경우 버전/빌드를 확인하며 WAL guard를 낮춰 통과시키지 않는다.

2. OpenAPI+ 사용등록·모듈·KOA Studio·선물옵션 모의투자를 준비한다. 최초 계약/월물과 callback type/FID·구독 방식·장 상태 범위를 KOA Studio에서 확인한다.
3. OCX와 호환되는 별도 bridge interpreter에 PyQt5를 설치하고 probe를 실행한다. 아래 변수는 사용자 PC의 실제 경로/검증한 코드로 바꾼다.

```powershell
$BridgePython = "C:\path\to\ocx-compatible\python.exe"
$FutureCode = "KOA_STUDIO_CONFIRMED_CODE"
& $BridgePython -m pip install -r adapters/kiwoom/requirements.txt
& $BridgePython adapters/kiwoom/collect.py --probe
& $BridgePython adapters/kiwoom/collect.py --output data/callbacks-session001.jsonl --codes $FutureCode --seconds 60
```

probe는 OCX 실제 생성과 GUI timer 실행만 확인하며 로그인하지 않는다. 수집 명령은 사용자가 공식 창에서 로그인하고 모의 서버 여부가 확인된 경우에만 시세 구독을 시도한다. 반환 보고서에 모의 확인·type별 callback 건수·spool fsync/drop/high-water/지연을 남긴다. 필요한 type을 받지 못하면 INCOMPLETE_COLLECTION으로 종료한다. `real_feed_verified=false`는 현장 대조가 아직 별도라는 의미다. OBSERVED는 실제 callback 수신 출처를 뜻하며 모의 시세와 거래소 실거래의 일치 보증이 아니다.

수집 실패 시 `error_details.stage`로 `MOCK_LOGIN`과 `MARKET_REGISTRATION`(화면 9000)을 구분한다. 등록 결과에는 `api_method`, `screen`, `registration_scope`, `return_value`, `return_type`을 기록하고 `registration_results`에 보존한다. `registration_scope=SELECTED_INSTRUMENTS`는 선택 종목에 대한 요청이며 전역 장 상태의 검증을 뜻하지 않는다. 이전 버전의 `SESSION_REGISTRATION`(화면 9001)은 빈 종목코드 등록 실패를 가리킨다. `None` 등의 미확인 반환값을 성공으로 바꾸지 않는다. 임의 예외 메시지나 예상하지 않은 COM 문자열은 출력하지 않는다.

`mock_login_confirmed`와 `login_error_code`는 로그인 자체의 결과다. 모의 로그인이 확인된 뒤 등록이 실패해도 로그인 성공 사실을 지우지 않는다. `registration_confirmed`는 선택 종목 등록 호출이 성공했을 때 true다. 이 값이 true여도 callback 수신과 실제 세션/FID 적합성 검증이 완료된 것은 아니다. `장시작시간`을 포함한 필수 callback이 없으면 계속 `INCOMPLETE_COLLECTION`이고, 코어는 장 상태가 UNKNOWN이면 이용을 차단한다. 종료 중 추가 오류가 발생하면 최초 오류를 유지하고 `secondary_errors`에 별도로 기록한다.

이전 버전이 `RuntimeError`만 보고한 경우에는 같은 PowerShell 창에서 수집 파일의 연결 이벤트를 확인한다. 이는 등록 API의 반환값을 복구하지는 않지만, 로그인과 수집 등록의 실패를 구분하는 근거가 된다.

```powershell
Get-Content -LiteralPath $CapturePath -Encoding UTF8 | ForEach-Object {
    $Frame = $_ | ConvertFrom-Json
    if ($Frame.event.kind -eq "CONNECTION") {
        $Frame.event | ConvertTo-Json -Compress
    }
}
```

4. `config/kiwoom.observed.example.json`을 복사해 공개 상품 코드·instrument ID·거래일·상품 규칙을 채운다. 검증 증거가 생긴 항목만 spec_verified/session_mapping_verified를 true로 바꾼다. 미확인 프로필로도 수신/보존할 수 있지만 market_status는 UNKNOWN이며 이용 불가다.
5. 별도의 core 터미널에서 수집 중인 파일을 따라 읽는다.

```powershell
.\.venv-core\Scripts\python.exe -m quant_system market-ingest --input data/callbacks-session001.jsonl --database data/kiwoom.db --run-id kiwoom-session001 --config config/kiwoom.local.json --follow --seconds 60 --manifest data/kiwoom-session001.manifest.json
```

파일은 collector가 생성한 뒤 연다. follower는 newline이 완성되기 전에는 frame을 발행하지 않는다. offline EOF의 불완전 frame은 원래 바이트를 보존하고 격리한다. 크기 한도를 넘는 파일은 처리를 중단하고 원본 spool 파일을 대조 대상으로 유지한다. follow 종료/transport fault 시 입력 gate를 닫는다. 정상 수집 빈도·구독 성공·actual source timestamp/ID 의미·지연의 합격 기준은 #33에서 실제 자료로 정한다.

로그인 정보·계좌번호·인증서·OTP·토큰은 입력/수집하지 않는다. 계좌·주문 callback은 등록된 시장 callback whitelist에서 제외한다. 공유 자료는 버전·오류 코드·개인정보를 제거한 시장 로그다.

## 검증 결과와 범위

PR #34 당시 Linux 64-bit / Python 3.12.14 / SQLite 3.53.1에서 **83개 unittest**가 통과했다. 기존 foundation 45개와 `tests/test_market.py`의 M01~M38이다. 당시 설치·소스 hash·합성 측정은 [검증 JSON](verification/kiwoom-20261104.json)에 기록했다. 지표 추가 이후의 전체 125개 검증은 [새 지표 안내](microstructure-features.md)를 따른다.

wheel은 별도 기본 venv에 `--no-index --no-deps`로 설치했고 PYTHONPATH를 제거한 채 시험했다. 설치한 CLI로 fixture 정규화·DB 무결성·manifest 재생을 실행했고 `pip check`도 통과했다.

합성 full-book 200건의 단일 순차 측정은 0.599초(약 334.0건/초)였다. raw+정규화 commit p95/p99는 2.660/3.528ms, callback 입력→consumer ACK는 4.588/6.039ms였다. raw 202건(연결/상태 포함), 메시지 203건, quarantine/gap 0, 마지막 book usable=true, 동일 manifest 배속별 trace 일치를 확인했다. WAL은 31,621,032바이트, PASSIVE checkpoint는 busy=0 및 7,675/7,675 frame이었다. 이 측정은 가상 이용 가능 시각의 **합성 단일 프로세스** 경로이며 실제 bridge·브로커 지연·burst 처리 능력을 측정한 것이 아니다. source 수신→코어 지연 0도 합성 시각 정의에 따른 값이다.

| 시험 | 내용 |
| --- | --- |
| M01–M06 | 원래 문자열·top-5·부호/단위·book/trade 분리·재전달/충돌·잘못된 depth·수량 0 |
| M07–M13 | 재접속·거래 상태·로컬 gap·producer epoch·HHMMSS/UTC/monotonic 역행 |
| M14–M20 | 과거 boundary·stale/expiry·지연 입력·복수 상품·미검증 프로필·origin 혼합·격리/대상 외 입력 |
| M21–M25 | profile/spec/상태 계보·오프라인 재생·CLI 전체 경로·부분 JSONL·follow·health 실패 |
| M26–M29 | heartbeat 손실·상태 범위 혼동 차단·새 writer/run·설정/입력 한도 |
| M30–M34 | fake COM port·모의 이외 접속 차단·대상 외 callback 제외·실제 큐 포화/fsync·시세 메서드 whitelist |
| M35–M38 | 정규화 후 ready_at·비Windows probe 거부·fsync 실패·잘못된 health 파일 차단 |

Qt ActiveX 자체를 fake port 시험으로 통과 처리하지 않는다. 실제 OCX·PyQt wheel/비트수·server flag·선물 구독/세션·계약/필드·수신 지연·실제 자료 replay 대조는 #33에 남아 있다. Issue #4의 실제 피드 완료 기준은 그 증거가 확보되어야 완료로 표시한다.

## 공식 참고

- [OpenAPI+ 사용등록·설치·모의 환경](https://www.kiwoom.com/m/customer/download/VOpenApiInfoView)
- [키움 상품별 API 경로](https://www.kiwoom.com/h/customer/download/VChannelHts4View?tab=3)
- [OpenAPI+ guide 1.7](https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.7.pdf)
- [Qt QAxBase·COM 호출](https://doc.qt.io/qt-6/qaxbase.html)

실제 bridge는 PyQt5를 사용한다. COM host 일반 근거와 설치된 PyQt5/OCX의 실제 호환성은 별도로 확인한다. 국내 개별주식선물·해외 선물·바이낸스 현물 어댑터로 지원 범위를 확장하지 않았으며 Layer 2는 후속 연구·구현 순서를 유지한다.
