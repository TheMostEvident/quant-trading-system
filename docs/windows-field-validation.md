# Extra Issue 1 (#33): Windows에서 남은 1~5 검증

이 문서는 실제 사용자 PC에서 수신·명세·저장·재생·중단/재시작 증거를 만드는 절차다. Linux 합성 시험이나 명령 추가만으로 #33을 닫지 않는다. Issue #8의 paper 실행은 별도 진행 판단 전까지 보류한다.

## 현재 상태와 참고 코드

Windows 코어 테스트 289개 발견/288개 통과/정상 제외 1개, 32-bit ActiveX 생성, GUI loop, Python 모의 로그인, 선택 종목 SetRealReg 반환값 0은 사용자 로그로 확인했다. A016C000은 당시 API의 최근월 코드 반환값이며 상품명·만기월·tick·거래일 의미를 모두 검증한 식별자는 아니다. KOA의 OPT50001 단건 응답도 실시간 호가 수신의 증거와 구분한다.

제공된 예전 주식 PyQt 코드에서는 SetInputValue → CommRqData → OnReceiveTrData 흐름을 참고했다. 대표 파일의 OnReceiveRealData 본문은 pass여서 선물 실시간·야간 세션의 작동 예제로 사용할 수 없다. 해당 파일을 실행하거나 계좌/주문 함수를 가져오지 않았다. 이번 변경도 COM 호출을 Qt GUI 스레드에 유지한다.

| 순서 | 준비된 도구 | PC에서 남은 확인 |
| --- | --- | --- |
| 1 | 초기 공개 시세 TR, 필터 전 real_type 집계, TR 응답 오류 기록 | 같은 코드로 KOA와 Python의 실제 갱신 비교 |
| 2 | 모의 서버·등록·세 유형 callback·원천 hash 보고 | 선물시세/선물호가잔량/장시작시간 실제 수신 |
| 3 | 미검증 프로필 생성과 누락 증거 검사 | 상품/월물/tick/단위/거래일/선물 장 상태 의미 대조 |
| 4 | bridge와 core 동시 실행, 원본/DB/manifest 교차 검사, 지표 재계산 | 실제 자료 묶음 생성과 결과 검토 |
| 5 | 종료 시 gate 차단, 별도 run 재시작 대조, 지연 분포 | 두 실제 세션 실행, stale/중단 증거와 지연 확인 |

## 1. 초기 TR과 실시간 이벤트를 구분한다

PR 병합 후 저장소 루트에서 실행한다. KOA에서 같은 상품의 값이 자동 갱신되는지 먼저 관찰하고 KOA를 종료한 뒤 Python 수집을 실행한다. 사용자 PC에서 확인한 32-bit `.venv-kiwoom`과 core `.venv-core`를 사용한다. 코드가 바뀌었다면 `$FutureCode`를 설치된 KOA/API에서 다시 확인한 값으로 바꾼다.

```powershell
git pull --ff-only origin main
.\.venv-core\Scripts\python.exe -m pip install -e .
.\.venv-core\Scripts\python.exe -m unittest discover -s tests -q

$FutureCode = "A016C000"
$CaptureStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$CapturePath = "data\callbacks-$CaptureStamp.jsonl"
.\.venv-kiwoom\Scripts\python.exe adapters\kiwoom\collect.py --output $CapturePath --codes $FutureCode --seconds 120 --bootstrap-tr --report "$CapturePath.report.json" --max-market-events 500
```

공식 로그인 창에서 모의 접속한다. `--bootstrap-tr`은 화면 9100에서 선택한 한 종목의 OPT50001을 한 번 요청한다. 고유 RQName·TR·화면이 모두 일치하는 OnReceiveTrData에서 공개 필드 네 개(종목코드·종목명·현재가·체결시간)만 읽는다. 설치된 응답 필드와 다르면 빈 문자열도 그대로 보고한다. 연속 조회·계좌 조회는 없다. 단건 TR 결과는 보고서의 진단 자료이며 source 시장 프레임이나 최신 book으로 주입하지 않는다.

| 보고서 | 해석 |
| --- | --- |
| `initial_tr.request_result` | CommRqData 호출 결과. int 0만 요청 접수 성공 |
| `initial_tr.response_count` / `response` | 해당 TR 응답 관측 수와 공개 문자열. 현재 시세라는 보증은 아님 |
| `observed_real_callbacks.total_before_filter` / `by_type` | 유형·종목 필터 전 OnReceiveRealData 집계. 대상 외 callback의 실제 필드/원문은 읽지 않음 |
| `filtered_unknown_type` / `filtered_unselected_code` | 기존 필터 때문에 제외된 수. 알 수 없는 type 이름은 최대 32개와 초과 집계만 보존 |
| `after_initial_tr_request` / `after_initial_tr_response` | 요청/응답 이후 관측 수. TR이 수신을 유발했다는 인과 증명은 아님 |
| `real_callback_counts` | 수집 대상 선물시세·선물호가잔량·장시작시간 건수 |
| `capture_sha256` | 최종 수집 파일 바이트에 대한 SHA-256 |

TR 응답만 있고 필터 전 callback도 0이면 `TR_RESPONSE_WITHOUT_REAL_CALLBACKS`, callback이 있지만 대상 수신이 0이면 `REAL_CALLBACKS_FILTERED_OR_INVALID`다. 필요한 유형이 빠지면 INCOMPLETE_COLLECTION을 유지한다. 오류 코드가 있는 TR 응답은 INITIAL_TR_RESPONSE 실패로 기록한다. 모의 로그인 성공 사실은 후속 실패로 지우지 않는다.

처음 비교는 주간에 KOA의 실제 갱신을 확인하면서 진행한다. 야간 거래소 운영과 OpenAPI+ 모의 시세 지원은 별도 항목이다. 야간 지원/코드/실시간 type이 확인되지 않았으면 시간을 바꾸거나 타입 필터를 없애 성공으로 표시하지 않는다. 같은 무수신 시도를 반복하기보다 위 세 집계를 비교한다.

## 2~3. 실제 수신과 상품·세션 근거를 확보한다

필수 세 유형이 모두 관측됐더라도 `real_feed_verified=false`는 유지한다. 사용자가 설치된 KOA 명세와 해당 상품의 공개 사양을 대조해야 한다. 가격이 움직인다는 사실만으로 다음 항목을 채우지 않는다.

```powershell
$TradingDate = "2026-11-04" # 실제 검증 세션의 거래일로 변경
$ProfilePath = "data\kiwoom-field-profile.json"
.\.venv-core\Scripts\python.exe -m quant_system market-profile-draft --code $FutureCode --trading-date $TradingDate --output $ProfilePath
.\.venv-core\Scripts\python.exe -m quant_system review-market-profile $ProfilePath
```

파일은 새 경로여야 한다. 생성기는 기존 template이 검증됐더라도 spec/session 플래그를 false로 초기화한다. review의 종료 코드 2는 근거 누락을 뜻한다. 아직 없는 값을 추정해서 채우지 않는다.

| 프로필 필드 | 필요한 근거 |
| --- | --- |
| `source_code`, `instrument_id`, `specification.contract_month` | 설치된 상품 목록의 상품명·정확한 계약/월물. 월물은 YYYY-MM |
| `specification.price_unit`, `tick_size`, `quantity_unit` | 이 어댑터의 국내 지수선물 가격 단위·고정 tick·계약 수량. 소수는 문자열로 입력 |
| `specification.verification_evidence`, `spec_verified` | 확인한 공개 상품 사양/설치 명세와 관측 대조 근거. 완료 후에만 true |
| `trading_date`, `specification.trading_date_evidence` | 해당 세션에 적용되는 거래일 근거. UTC 날짜/PC 날짜를 야간 거래일로 자동 대체하지 않음 |
| `status_codes`, `mapping_evidence`, `session_mapping_verified` | 수신 FID 215의 실제 코드·선물 세션 범위·의미를 대조한 근거. 주식 장 상태를 선물에 그대로 적용하지 않음 |

`session_mapping_verified`를 true로 바꾸어도 pending 근거는 review에서 실패한다. review는 근거의 형식과 누락을 확인하며, 사용자가 적은 설명의 진실성을 독립 인증하는 도구는 아니다. `quantity_unit=CONTRACT`, `price_encoding=SIGNED_MAGNITUDE`, top-five 범위는 현재 어댑터의 지원 조건이다. 실제 상품이 다르면 프로필 플래그를 바꾸는 대신 어댑터를 수정해야 한다.

미검증 프로필로도 raw 보존과 진단 실행은 가능하다. 코어의 UNKNOWN 상태·지표/quote gate 차단은 유지되고 현장 audit는 통과하지 않는다. 의미가 확인된 프로필로 **새 세션**을 실행해야 한다. 이미 저장한 run의 프로필을 사후 변경하지 않는다.

## 4. 실제 source·DB·재생·지표를 함께 확인한다

검증한 프로필을 준비한 뒤 core에서 실행한다. bridge를 별도 32-bit 프로세스로 실행하고 core는 즉시 파일을 follow한다. 뒤늦은 offline import를 당시의 처리 지연으로 표시하지 않는다. 초기 진단은 100건부터 시작하고 실제 수신량에 맞춰 별도 세션을 측정한다.

```powershell
$SessionStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SessionA = "data\field-$SessionStamp-a"
.\.venv-core\Scripts\python.exe -m quant_system market-field-run --bridge-python .\.venv-kiwoom\Scripts\python.exe --profile $ProfilePath --output-dir $SessionA --seconds 120 --max-market-events 100 --fsync-budget-ms 1000
```

기간은 10~1800초, 대상 시장 이벤트는 1~2000건이다. 로그인 시간도 기간에 포함되므로 공식 창에서 지체하면 시간이 부족할 수 있다. 필수 유형을 받기 전에 건수 한도에 도달하면 여전히 실패다. 소스/manifest 검사는 파일당 64MiB 상한이 있어 처음에는 짧게 수집한다.

묶음에는 `session.json`, 고정 `profile.json`, `callbacks.jsonl`과 health sidecar, `collector.json`, `collector-console.log`, `history.db`, `ingest.json`, `manifest.json`, `audit.json`이 생성된다. 각 실행은 새 디렉터리·새 run·새 producer epoch를 사용한다. 계좌/인증 정보는 입력하지 않는다. 공유 전 콘솔 로그는 직접 확인하고, 필요한 공개 시세/검증 요약만 전달한다.

audit는 다음을 교차 확인한다.

- collector 보고서와 source 파일 hash·필수 type별 건수·프레임 카운터·epoch·수신 UTC/monotonic 순서.
- 모든 source 줄의 원본 hash와 DB capture의 hash/중복 건수. core watchdog 자료는 별도 출처로 구분.
- SQLite 무결성, run의 고정 프로필, DB에 저장된 manifest의 정확한 바이트·store/run 연결.
- book/trade 정규화, tick 격자, 품질 플래그, 실제 수신→ready 지연, 각 상품의 이용 가능한 CONTINUOUS/SYNCED book.
- 동일 manifest의 배속 1/1000 재생 일치와 저장된 지표의 독립 재계산 일치.

`baseline_checks_passed`가 true여도 첫 묶음의 `controlled_process_restart`는 PENDING, `field_checks_passed`는 false다. 아직 중단/재시작 대조가 없기 때문이다. 형식/무결성이 깨진 자료는 예외와 종료 코드로 실패하며, orchestration 오류는 `field-error.json`과 기존 자료를 남긴다. synthetic/fixture 자료를 Windows 보고서로 바꾸어 현장 통과시키지 않는다.

## 5. 중단·새 run 복구와 성능을 확인한다

첫 수집이 정상 시세를 받은 뒤 종료되면 마지막 disconnect와 core gate 차단을 저장한다. 다음 명령은 같은 PC에서 다시 공식 모의 로그인해 새로운 연결·상태·book을 확인한다. 이전 run에 append하지 않는다.

```powershell
$SessionB = "data\field-$SessionStamp-b"
.\.venv-core\Scripts\python.exe -m quant_system market-field-run --bridge-python .\.venv-kiwoom\Scripts\python.exe --profile $ProfilePath --output-dir $SessionB --seconds 120 --max-market-events 100 --fsync-budget-ms 1000 --previous-session $SessionA

.\.venv-core\Scripts\python.exe -m quant_system audit-market-session $SessionB --previous-session $SessionA --fsync-budget-ms 1000 --output "$SessionB\audit-repeat.json"
```

두 묶음 모두 기본 검사를 통과하고, 첫 묶음의 정상 이용 이후 GAP/STALE 및 종료 disconnect, 두 번째의 별도 producer epoch·더 늦은 시작·새 정상 book이 있어야 controlled_process_restart가 PASS다. 이는 **수집 프로세스 정상 종료/새 실행** 검증이며 물리 네트워크 단절·강제 종료·정전 시험을 대체하지 않는다. 그 시험이 완료 기준에 필요하면 별도 근거를 남긴다. 미검증 세션/상품이나 새 상태·book이 없는 동안 계속 차단되는 것도 확인한다.

현재 프로필의 max_transport_delay_ms=1000에 맞춰 첫 실행의 fsync 예산을 1000ms로 기록한다. 지난 사용자 로그의 max_enqueue_to_fsync_ms=4134.619는 이 기준에서 실패다. 원인을 해결하지 않고 사후 audit 예산을 키우면 declared_latency_budget 검사도 실패한다. 수집 시점에 예산을 바꾼 새 실행은 구분해 기록하며, 코어의 stale/transport guard를 자동 완화하지 않는다.

collector의 fsync 지표에는 큐 대기부터 실제 flush/fsync까지의 최대값과 고정 bucket p95/p99 **상한값**, high-water·drop·fsynced/enqueued를 기록한다. core 측 수신→ready 지연은 별도 분포다. 최초 짧은 수집은 고부하 성능 보증이 아니다. `ingest.json`의 WAL/checkpoint와 오류, 실제 이벤트 건수·구성·시간, 반복 시도 조건도 함께 검토한다. raw 수신 시각은 로컬 callback 관측 시각이며 거래소 전송 시각/네트워크 지연과 동일하지 않다.

프로필 의미·현장 자료·측정 범위를 사람이 검토한 뒤 #33 상태를 결정한다. 도구는 Issue를 자동 종료하거나 #8의 주문/실행 경로를 켜지 않는다.

## 도구 자체의 검증 범위

Linux Python 3.12.14 / SQLite 3.53.1에서 소스와 별도 venv에 설치한 wheel 각각 전체 312개 시험을 통과했다. 새 23개 시험은 초기 TR과 live book 분리, 필터 전 집계, 초기 실패 보고, raw/report 위조 불일치, fixture/origin 구분, 고정 지연 예산, 별도 run 조건, producer 종료 후 follow 차단 등을 확인한다. 설치본의 39개 Python 파일도 소스와 바이트가 일치했다. [검증 기록](verification/extra33-tools-20261104.json)에 실행 범위와 hash를 남겼다. 이 결과는 실제 Windows ActiveX 수신 시험이나 새 revision의 사용자 PC 통과 결과가 아니다.
