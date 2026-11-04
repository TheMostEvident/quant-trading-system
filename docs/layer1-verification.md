# Issue #9: Layer 1 통합·독립 재계산·장애·한도 검증

국내 선물 seed와 STATIC 정책으로 **합성 callback → 원천 저장 → 호가/체결 정규화 → 지표 → 신호 → 영속 주문 claim → 지연 모의 체결 → 원장**을 한 번에 실행한다. Layer 0·2·3, 계좌 접근, 실제 주문 전송은 필요하지 않다. 실제 환경 검증은 [Extra Issue 1 (#33)](https://github.com/TheMostEvident/quant-trading-system/issues/33), 집행 가정/비용/지연 대조는 [Extra Issue 2 (#43)](https://github.com/TheMostEvident/quant-trading-system/issues/43), 실제 체결·계좌 대조 provider의 보장은 [Extra Issue 3 (#45)](https://github.com/TheMostEvident/quant-trading-system/issues/45)에 남긴다.

## 실행

core 설치 환경에서 실행한다. 각 출력은 새 경로를 사용한다.

```powershell
.\.venv-core\Scripts\python.exe -m quant_system layer1-scenario tests\fixtures\layer1.synthetic.json --output-dir data\layer1-demo-001
.\.venv-core\Scripts\python.exe -m quant_system audit-oms data\layer1-demo-001 --output data\layer1-demo-001\audit.json
.\.venv-core\Scripts\python.exe -m quant_system layer1-scenario data\layer1-demo-001\inputs.json --output-dir data\layer1-demo-002
.\.venv-core\Scripts\python.exe tools\verify_layer1.py --output-dir data\layer1-checks-001
.\.venv-core\Scripts\python.exe -m unittest discover -s tests -q
```

`layer1-scenario`는 `history.db`, 원래 `inputs.json`, 파생된 `effective-config.json`, `trace.json`, `decisions.json`, `manifest.json`, `summary.json`을 만든다. `audit-oms`는 원장을 별도 코드로 재계산하고 불일치 때 종료 코드 2를 반환한다. 형식/무결성이 깨진 입력도 실패로 종료한다. 검증 실패가 producer의 실제 장애를 자동 해제하지 않는다.

`verify_layer1.py`는 소스 저장소의 새 검증 41개를 실행하고 `report.json`에 테스트 ID·성공/실패·시간을, `tests.log`에 상세 결과와 실패 traceback을 보존한다. 0개 발견·skip·오류·실패를 정상 통과로 표시하지 않는다. 설치한 패키지를 검사할 때는 그 interpreter로 실행하고 PYTHONPATH를 제거한다. 이 도구가 실행하는 테스트는 모두 합성 환경용이다.

## 통합에서 확인한 연결 조건

- 수집기가 보존한 명세 binding과 원장이 사용한 상품 명세를 정확히 대조한다. `source_bindings`를 명시한 합성 실행에서는 같은 불변 artifact를 공유하며, 수량 단위·가격 단위·tick·승수·증거금 규칙이 서로 다르면 거부한다. 원래 template와 유효 설정을 각각 보존한다.
- `SOURCE_TIME_UNKNOWN`, `SOURCE_SEQUENCE_UNAVAILABLE`, `SOURCE_EVENT_ID_UNAVAILABLE`는 **SYNTHETIC 경로에서만** 거래소 메타데이터 부재를 뜻하는 정보성 경고다. source/메시지에서 지우지 않는다. 명시적 로컬 시뮬레이션 clock과 가용 경계를 사용하는 시험에서 이 세 경고를 차단 사유와 구별한다.
- STALE·GAP·CLOCK_UNVERIFIED·SESSION_UNVERIFIED·INSTRUMENT_UNVERIFIED 및 미지의 flag는 계속 차단한다. OBSERVED 경로에는 위 예외를 적용하지 않으며 현재 집행기와 통합 runner는 OBSERVED를 실행하지 않는다. 원천 clock domain이 바뀌면 기존 주문의 clock으로 추정해 체결하지 않는다.
- policy·실제 생성 AccountState/PositionState·현재 book·signal artifact가 저장되고 소비자에게 전달된 t/W/D 경계에서만 `submit_at_boundary()`를 호출한다. 과거 정상 호가·정책을 선택해 최신 만료/무효화를 우회하지 않는다. 고정 총 목표를 현금/증거금 한도에 맞춰 임의 증액/축소하지 않는다.
- 상태/단위/시계·한도·UNKNOWN 차단, FULL/fsync·원천 보존·단일 writer·예약 보수성은 유지한다. 검증 통과를 위해 실제 상품/세션 플래그를 켜지 않는다.

## 독립 재계산의 범위

`validation/reference.py`는 production의 execution engine·ledger·matcher를 import하지 않는다. 저장된 OMS 입력/결과와 원본 capture의 일치를 확인한 뒤, 접수된 고유 fill과 MARK/SETTLE/BUST를 effective 시각/sequence로 정렬해 별도 Fraction FIFO 회계를 수행한다.

| 대조 | 확인 내용 |
| --- | --- |
| 저장 연결 | input/raw hash·projection hash·최종 history 연결·summary/trace digest·DB/manifest/store/run 연결 |
| 경제 재계산 | 매 입력 단계의 잔고/포지션/equity, 최종 잔고·포지션·정산·초기/유지 증거금·가용 증거금 |
| 체결 인과성 | 영속 claim 뒤 체결, report 가용 시각, clock domain, 수량 step·tick·지정가·중복 식별 |
| 모의 집행 제약 | 해당 원본 관측·주문 지연·보고 지연·stale·만기·가격 level·참여율·공유 유동성 상한·fee/tax |
| 재생 | 같은 저장 입력의 state/trace/decision hash, 지표 독립 재계산, manifest 배속별 전달 재생 |

매칭 검사는 선언한 모형의 **제약과 상한 대조**이며 독립 거래소 시뮬레이터나 실제 지정가 대기열 모델이 아니다. 실제 미관측 유동성·체결 확률을 추정하지 않는다. 원본을 포함한 모든 자료를 바꿀 수 있는 운영자의 위조 방어를 보장하지 않는다.

경제 입력이 있는 미일치 reconciliation은 이 auditor가 통과 처리하지 않는다. 완전한 실제 broker 조회를 인증하는 기능도 아니다. fault가 의도대로 기록되고 경제 재계산이 일치하면 audit는 통과할 수 있으므로 `producer_faults`와 현장 검증 상태를 별도로 읽어야 한다. audit의 `verified=true`는 실제 운영 health의 해제를 뜻하지 않는다.

현물의 소수 수량·base/quote/제3자산 비용은 자산별로 따로 재계산한다. BTC·USDT·BNB 비용을 임의 합산/환산하지 않는다. 이 범용 회계 검사는 Binance 연결이나 현물 진입/집행 모형 지원 완료를 뜻하지 않는다.

## 고정 fixture와 손으로 계산한 결과

합성 기간은 2026-11-05 00:00:00~00:00:00.900 UTC이며 현재 시장을 관찰한 시간이 아니다. 입력은 callback 15개(연결/상태·book 11개·trade 1개·단절)와 timer/policy/mark를 포함한 총 20개다. 정책의 진입 3회 관측/200ms 조건으로 진입·청산·숏 진입을 검증한다. 승수 1000, 계약당 비용 20, 주문 지연 10ms·통보 지연 5ms는 시험값이다.

| 단계 | signed 계약 | KRW 현금 | KRW 평가자산 |
| --- | --- | --- | --- |
| 초기 | 0 | 1,000,000 | 1,000,000 |
| 101에 2계약 매수, 비용 40 | 2 | 999,960 | 999,960 |
| 100.9에 2계약 청산, 비용 40 | 0 | 999,720 | 999,720 |
| 100.9에 숏 2계약, 비용 40 | −2 | 999,680 | 999,680 |
| 100 평가 | −2 | 999,680 | 1,001,480 |

[독립 예상표](../tests/fixtures/layer1-expected.json)에 모형 실행 결과를 복사하지 않고 숫자를 적었다. 최종 초기/유지 증거금은 40,000/30,000 KRW, 비용은 120 KRW, 거래 명목가치 합계는 605,600 KRW다. 이는 합성 장부 검증 값이며 전략 수익률의 증거가 아니다.

## 장애·위험 한도 검사

| 시험 집합 | 재현하는 상황 |
| --- | --- |
| 통합 입력 | 중복 callback, sequence GAP, source monotonic 역행, 지연/stale, 단절, HALT, 정책 만료 |
| 위험 한도 | 종목 수량·계좌 notional·증거금 부족, 다른 상품의 노출, 살아 있는 주문·예약 수량 |
| 주문 통보 | 고유 fill 중복/충돌·역순 누적 통보·취소 ACK 뒤 지연 fill·결과 불명·재전송·재시작 |
| 손익/범용 | 롱/숏·정산 전후 equity 보존·BUST 비용 반환·소수 현물/자산별 수수료 |
| 부정 자료 | summary 변경, projection과 그 hash를 함께 변경, 실제 원천/미검증 profile 투입 |

입력 EOF에서 pending fill 통보를 강제로 반영하거나 예약을 해제하지 않는다. 취소 ACK가 와도 미확정 수량을 유지하고 늦은 fill을 한 번만 반영한다. 단절/결과 불명/재시작에서 새 명령을 만들기 위해 보유나 예약을 0으로 초기화하지 않는다.

## 측정과 완료 경계

summary에는 단계 처리 지연의 고정 bucket p95/p99 상한과 최대값, source 처리/중복/GAP/격리 수를 기록한다. audit에는 dispatch→fill의 **합성 clock 지연**, 종목별 체결 비율, 자산별 비용·회전 명목가치·초기 자산 대비 회전 비율·평가 손익을 기록한다. step 처리 지연은 로컬 계산/저장 시간이며 브로커 지연·실제 HFT 처리량이 아니다. 각 비율의 분모와 자산을 구분한다.

독립 합성 경로와 회귀 검증이 통과해도 실제 source·상품·비용·계좌·대조 provider는 #33/#43/#45에서 별도 확인해야 한다. 매칭 상태 crash-resume·실계좌 provider·live/shadow 연결·현물 집행은 이 검증으로 지원 완료를 선언하지 않는다. Layer 1의 실제 운영/수익성 완료와 독립 소프트웨어 검증 완료를 구분한다.

Linux Python 3.12.14 / SQLite 3.53.1에서 소스와 별도 설치본 각각 전체 **384개 시험**이 통과했다(새 41개 포함). 설치한 45개 Python 모듈이 소스와 바이트 일치했고 offline build/install·pip check·CLI/DB/독립 audit/저장 입력 재실행도 통과했다. [개별 시험 ID·결과·hash·측정 기록](verification/layer1-20261105.json)에 범위를 남겼다. 이 revision의 Windows 시험과 현장 검증은 별도다.
