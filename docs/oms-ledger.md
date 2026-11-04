# 주문 상태·체결 원장·대조 (Issue #7)

`execution/`은 명시적인 신호·주문 command·ExecutionReport를 처리하고, 실제로 입력된
체결만 원장에 반영하는 **오프라인 OMS 코어**다. SQLite에 입력·결과·상태·전송 command를
보존한다. 브로커 전송 함수와 호가 기반 자동 체결 생성은 없다. 체결 지연·유동성·비용의
모의 집행 모델은 #8이며 실제 키움 Windows/OCX·계좌·상품 검증은 #33이다.

#8의 독립 합성 집행기는 [모의 집행 안내](paper-execution.md)를 따른다. 실제 자료로 지연·비용·유동성 가정을 대조하는 항목은 Extra Issue 2 (#43)로 분리한다. #33 미완료만으로 독립 개발을 보류하지 않는다.

현재 실행 모델은 `synthetic-fifo-v1`, `data_origin=SYNTHETIC`,
`SYNTHETIC_REPLAY`만 허용한다. 첫 대상은 선형 선물 CONTRACT/NET의 합성 회계다.
고정 계약당 증거금·FIFO 손익 배분·동시각 이벤트 순서는 **시험 규칙**이며 국내 선물이나
다른 브로커의 실제 장부·증거금·정산·체결 통보 규칙을 확인했다는 뜻이 아니다.

## 실행과 파일

```bash
quant-foundation oms-scenario tests/fixtures/oms.synthetic.json --output-dir data/oms-demo
quant-foundation integrity data/oms-demo/history.db
quant-foundation replay data/oms-demo/manifest.json --run-id oms-replay
quant-foundation oms-scenario data/oms-demo/inputs.json --output-dir data/oms-replay
```

출력 폴더는 비어 있어야 한다. `history.db`, `inputs.json`, `trace.json`,
`manifest.json`, `summary.json`을 생성한다. trace/state SHA는 같은 코드·입력에서
동일하며 새 DB의 store ID가 포함되는 manifest 자체의 동일성은 주장하지 않는다.
manifest 전달 재생은 배속 1/1000에서 비교한다. 원장 자체의 검증은
`ExecutionJournal.verify()`의 입력 순차 재계산과 별도 수작업 예상표를 사용한다.

| 파일 | 역할 |
| --- | --- |
| execution/ledger.py | exact Fraction 연산, FIFO lot·자산별 분개·평가·명시적 합성 명세 |
| execution/engine.py | 입력 하나의 순수 상태 전이·command 생성·대조 |
| execution/journal.py | raw-first 기록·원자적 상태/command commit·중복 receipt·가용 경계 |
| execution/snapshots.py | v0.2 AccountState/PositionState·참조·원장 version |
| execution/scenario.py | 제공된 합성 입력 실행·저장·전달/재생 검사 |
| config/oms.synthetic.json | 합성 선형 선물·초기 현금·고정 증거금·계좌 gross 한도 |

## 지속성과 전송 경계

`ExecutionJournal`은 기존 `JournalStore`의 단일 writer lease, WAL/FULL,
로컬 저장·백업·무결성 프로필을 사용한다. 공통 storage version 1과 v0.2 봉투는
변경하지 않고 독립 version 1의 `oms_events`와 `oms_projection`을 추가했다.
이전 DB에 OMS를 처음 연결하면 초기 테이블을 추가하지만 기존 OMS version의
자동 migration이나 기존 포지션의 임의 초기화는 지원하지 않는다.

1. OMS 입력 JSON을 Capture로 먼저 commit한다. `OMS_INPUT_JSON`은 호출된 dict의
   정규 JSON이며 브로커의 원본 wire bytes라고 표시하지 않는다. Capture의 ns는
   합성 archive ordinal이고, 거래 시간 계산은 입력 안의 명시적 clock을 사용한다.
2. 한 transaction에서 불변 receipt·입력/결과 SHA·이벤트 artifact와 OMS projection을
   commit한다. projection 안의 command queue·주문 예약·포지션·자산 분개가 함께 바뀐다.
3. 브로커가 사용할 수 있는 command는 별도 DISPATCH 입력으로 **먼저 claim**한다.
   claim을 commit한 뒤 caller에게 반환하며 실제 전송 함수는 제공하지 않는다.

commit 전 예외/강제 종료에서는 raw만 남고 주문 예약·command·체결 효과는 남지 않는다.
같은 event_id와 같은 입력은 원래 결과를 반환하며 다른 입력은 충돌 receipt를 추가하고
UNRECONCILED로 멈춘다. DISPATCH의 재시도는 `ALREADY_CLAIMED/DO_NOT_RESEND`를 반환해
같은 command payload를 두 번 전송하도록 만들지 않는다. claim 뒤 응답을 받지 못했다면
자동 재전송하지 않는다. 이것은 외부 증권사까지의 exactly-once 보장이 아니다.

다시 연 journal은 저장 보유량을 복원하지만 대조 전 신규 주문을 막는다. 재시작을
이유로 포지션·예약을 0으로 만들지 않는다. 공통 foundation health fault도 신규 command를
차단하며, OMS 대조만으로 다른 공통 장애의 health를 임의 해제하지 않는다.
`verify()`는 초기 config부터 receipt·재시작/fault context를 순서대로 적용하고 각 state/result
및 최종 projection을 대조한다. 운영자의 DB 변조 방어를 보장하지는 않는다.

## 입력과 signal 연결

#9의 [통합·독립 검증](layer1-verification.md)은 실제 생성한 policy/book/account/position을 전달한 경계에서 signal을 연결한다. 합성 config의 선택 항목 `source_bindings`가 있으면 수집/원장이 정확히 같은 명세 binding artifact를 공유해야 한다. 미확인 단위나 승수를 자동 변환하지 않는다. 실제 broker 대조의 완전성·시각·ID 근거는 Extra Issue 3 (#45)에서 확인한다.

입력은 `event_id`, `kind`, `at`(UTC 6자리),
`clock={producer_id,epoch,monotonic_ns}`, `instrument_id`, `data`다.
동일 epoch에서 시간 역행을 허용하지 않으며 clock domain 변경은 대조를 요구한다.

| kind | 처리 |
| --- | --- |
| TARGET | 고정 정책 신호의 총 목표를 현재 실제 보유와 비교해 한 주문을 예약 |
| DISPATCH | NEW/CANCEL command를 한 번 claim; 브로커 호출 없음 |
| CANCEL / UNCERTAIN | 취소 요청 또는 전송 결과 불명; 보유를 바꾸지 않음 |
| REPORT | v0.2 ExecutionReport의 접수/체결/취소/거절·정정 처리 |
| RECONCILE | 완전한 체결·미체결·포지션·자산·증거금 snapshot 대조 |
| MARK / SETTLE | 평가가 변경 또는 명시적인 합성 선물 정산 |
| TICK | 최초 intent의 기간/TTL 기준으로 미전송 주문 만료 또는 취소 요청 |
| HALT / RESUME / DISCONNECT | 신규 주문 차단·거래 중단·연결 불명 관리 |

TARGET은 `static-obi-v1` signal과 v0.2 STATIC policy·현재 BOOK·소비자 seen_at,
동일 domain의 quote_clock, intent ID/유효시각·지정가·자산별 fee reserve를 요구한다.
신호의 actual_position_qty가 현재 원장과 달라졌으면 차단한다. ENTER/INCREASE는
명시적으로 entry_allowed=true이고 원래 quote_ref를 가져야 한다. EXIT/REDUCE도
실행 가능한 새 호가·세션·가격·예약 검사를 거친다. HALT/UNKNOWN은 주문 전송 허가가 아니다.

Python의 `submit_at_boundary()`는 signal artifact와 policy/quote를 저장소에서 읽는다.
policy/book은 version이 있어야 하고 t/W/D에서 실제 전달·선언된 입력이어야 한다.
가용한 과거 정책을 골라 최신 정책을 우회할 수 없으며, 최신 book이 stale/만료/무효화되면
예전 정상 book으로 돌아갈 수 없다. signal의 모든 input_refs도 같은 경계 안에 있어야 한다.
정책 만료는 신규 증가를 막지만 최신의 만료 정책으로 기존 감소를 검토하는 것은 허용한다.

직접 `apply(TARGET)`는 명시적 합성 bundle API다. JSON의 seen_at·signal·verified 단어가
실제 관측·계좌 인증을 증명하지 않는다. 실제 소비자·계좌 adapter를 연결할 때는 위 경계,
정책 ownership, 원천 clock 및 브로커 capability를 검증해야 한다. 현재 계좌/API 인증이나
사용자 자격 증명을 받는 코드가 없다.

## 주문 상태와 예약

주문은 PENDING_SEND → SENDING → WORKING/PARTIALLY_FILLED → FILLED 또는
CANCEL_PENDING → CANCELED/REJECTED/EXPIRED를 표현한다. 응답 유실은 UNKNOWN이다.
포지션 projection은 FLAT/PENDING/PARTIALLY_FILLED/OPEN/CLOSING/UNKNOWN/HALTED를 구분한다.
접수·주문 의도·취소 수량은 실제 체결 수량이 아니다.

상품당 하나의 살아 있거나 종료 확인이 안 된 주문만 허용한다. 현재 보유 q와 목표 Q의
차이 abs(Q−q)를 주문 수량으로 쓰며 Q를 추가 주문량으로 해석하지 않는다. 반대 부호의
목표는 거절하고 기존 포지션의 감소/0 목표를 먼저 요구한다. 남은 예약이 있는 동안
다른 close 주문을 보내지 않는다. 완전 체결이나 대조로 flat이 확인된 후 반대 방향을
검토한다. #6의 반대 방향 N/기간 필터를 OMS가 생략하거나 대신 예측하지 않는다.

취소 응답 후에도 지연 체결이 올 수 있으므로 `finalized=false`를 유지한다. 예약은
원 주문 수량−확인한 체결량을 보수적으로 유지하고, 실제 체결·미체결·잔고의 완전한 대조
전에는 새 주문/재시도를 하지 않는다. qty 전체의 체결 상세가 확보된 FILLED는 예약을 해제한다.
아직 전송 claim을 하지 않은 주문만 로컬 취소/만료로 안전하게 해제할 수 있다.

주문 재시도는 같은 intent ID의 최초 anchor quote·방향·총 목표·생성 clock·TTL·만료·
max_retries·slippage를 보존한다. 새 호가를 넣어 가격 추격/기간 한도를 초기화하지 않는다.
NEW command를 실제 claim할 때도 TTL·intent 만료·신규 진입 정책 만료를 다시 검사한다.
응답 불명·취소 중·부분 체결에서는 원 주문을 확인하지 않은 재전송을 하지 않는다.
각 지정가의 tick·스프레드·상품/계좌 capability·수량·주문 notional·계좌 gross·증거금·
fee/현물 잔고 예약을 검사한다. 감소 주문은 기존 margin shortfall만으로 자동 금지하지 않는다.

## 체결 식별·순서·대조

fill identity는 실행 scope·계좌·broker_order_id·fill_id의 tuple이다. 같은 ID·같은
체결 상세는 한 번만 반영하며 같은 ID의 다른 가격/수량/비용은 격리·대조한다.
별개의 fill ID는 수량/가격이 같아도 별도 체결이다. 브로커 ID를 다른 로컬 주문에
재사용하지 않는다. 누적 수량의 큰 값만으로 수량/현금을 추정하지 않고 각 last_fill을 요구한다.
역순 누적 통보는 기존 fill을 되돌리지 않으며 상세가 누락되면 UNRECONCILED다.

합성 last_fill extension은 `executed_at`, `execution_sequence`, 검증된 `clock`을
요구한다. first transport claim보다 이전이거나 미래인 체결, 비정수 CONTRACT는 격리한다.
원장 재계산은 executed_at/sequence 순서로 적용해 지연 도착한 과거 fill이 FIFO·정산에
반영되도록 한다. 같은 UTC에서는 합성 규칙으로 FILL → MARK → SETTLE 순서를 쓴다.
이 source 순서·시계를 실제 피드가 제공한다고 가정하지 않는다. 없는 값은 실제 adapter에서
추측하지 말고 UNKNOWN/대조로 넘겨야 한다.

RECONCILE은 같은 execution_scope의 증가 watermark, 현재 as_of, complete=true,
transport_quiesced=true와 전체 실행 상세·open_orders·모든 설정 상품의 실제 수량·
자산별 잔고·합성 margin 수치를 요구한다. 살아 있는 전송이 없고 watermark까지 체결이
완전하다는 **provider의 근거**가 필요하다. 이 boolean만 써서 실제 브로커의 완전성을
증명할 수는 없다. #33/후속 adapter에서 보장 범위를 검증해야 한다.

기존 fill을 누락하거나 누적 체결 증거를 지울 수 없다. 로컬에 없는 주문/불일치 잔고를
자동 채택하지 않으며 MISMATCH 상태로 멈춘다. 새로 제공된 유효 체결 상세는 반영하고
포지션/잔고가 일치하지 않으면 예약은 보수적으로 유지한다. source가 대조 barrier 이후의
이미 종료 확인한 주문에 새 fill을 통보하면 확인 가능한 실제 체결은 기록하고 다시 멈춘다.
broker protocol 자체가 깨지는 경우까지 포지션 비역전을 보장한다고 주장하지 않는다.

지원 정정은 CORRECTION·reason=BUST·알려진 fill_key의 correction_of다. 원본 receipt는
보존하고 해당 fill을 busted로 표시해 영향을 받는 FIFO/정산을 재계산한다. 자산별 반대
분개 delta를 불변 결과에 기록하고 비용·수량을 다시 반영하지 않는다. 재대조가 필요하다.
임의 가격 정정·미상 체결 취소는 격리하며, 후속 정정 adapter의 명세가 필요하다.

## 장부 규칙과 상품 경계

| 항목 | 합성 구현 |
| --- | --- |
| 선물 수량 | signed CONTRACT, LINEAR/NET; abs 수량과 승수 포함 notional을 따로 검사 |
| 선물 현금 | 명목가치 전액을 차감하지 않음; 수수료/세금, FIFO 실현손익, 정산만 반영 |
| 미정산손익 | 각 lot의 signed qty × (mark−basis_price) × multiplier |
| 정산 | 해당 손익을 현금 반영하고 각 lot basis를 정산가로 갱신; 원래 entry는 보존 |
| 평가자산 | collateral wallet + 미정산손익; 이미 지급한 비용/정산을 재차 가감하지 않음 |
| 증거금 | 초기 사용·유지·살아 있는 OPEN 예약·fee reserve를 구분; 비용으로 차감하지 않음 |
| 현물 | CASH·SHARE/BASE_ASSET; gross 매입/매도 대금과 실제 기준자산 변동을 기록 |
| 비용 | FEE/TAX 자산별 보존; 음수 rebate 허용; base 자산 비용은 실제 현물 재고도 감소 |

모든 수량·가격·분개·손익은 plain decimal→Fraction으로 계산한다. FIFO lot 가격을 직접
사용하므로 평균가의 표시 반올림이 현금 분개에 쓰이지 않는다. 표시 평균은 40자리
Decimal이고 원장은 lot 원본을 유지한다. 실거래의 통화 단위 절사·반올림·세금·거래소
정산가는 확인하지 않았으며 실제 규칙 adapter가 필요하다. 음수 선물 가격도 허용하고
notional은 abs 값으로 검사한다.

선물은 한 계좌의 동일 collateral asset, 현물은 각 base 자산당 하나의 inventory를
지원한다. 초기 inventory는 flat이며, 기존 외부 보유를 씨드로 넣는 기능은 이 버전에 없다.
외화·토큰 비용을 임의 환산하지 않는다. 미평가 자산은 그대로 보존하고
VALUATION_UNRESOLVED로 표시해 신규 증가를 막는다. 현물에 선물 margin·숏을 적용하지 않는다.
현물 소수 수량/비용의 장부 표현을 검증했지만 **#6 현물 진입/OBI 유지 전략, Binance
연결 또는 실제 현물 거래 지원 완료를 뜻하지 않는다.**

`publish_snapshots()`는 v0.2 AccountState를 한 번 만든 뒤 각 PositionState가 같은
account_state_ref를 참조하게 한다. 원장 버전과 소스 artifact 계보를 보존하고
first_fill_ref/first_fill_clock을 추가해 #6이 최초 체결 기산점을 읽을 수 있게 한다.
중간 메시지 발행 실패는 실제 ledger commit을 되돌리지 않으며 같은 version/message ID로
다시 발행할 수 있다. 원장과 브로커가 MATCHED가 되었다는 실환경 인증은 별도다.

## 고정 검증과 다음 단계

[수작업 기준표](../tests/fixtures/oms-expected.json)는 signed 보유·현금·평가자산·증거금을
독립적으로 적었다. [20단계 입력](../tests/fixtures/oms.synthetic.json)의 모든 체결은 명시적인
브로커 fixture이며 책 호가를 보고 OMS가 생성한 체결이 아니다.

| 단계 | signed 보유 | KRW 현금 | KRW 평가자산 |
| --- | --- | --- | --- |
| 초기 주문·접수 | 0 | 1,000,000 | 1,000,000 |
| 101에 1계약·비용 20 | 1 | 999,980 | 999,980 |
| 취소 응답 뒤 나머지 1계약·비용 20 | 2 | 999,960 | 999,960 |
| 102 평가 | 2 | 999,960 | 1,001,960 |
| 102 정산 | 2 | 1,001,960 | 1,001,960 |
| 103에 전량 청산·총 비용 40 | 0 | 1,003,920 | 1,003,920 |
| 접속 해제·완전 대조 | 0 | 1,003,920 | 1,003,920 |
| 101에 숏 2계약·비용 40 | −2 | 1,003,880 | 1,003,880 |
| 100 평가 | −2 | 1,003,880 | 1,005,880 |

**282개 시험(기존 198 + OMS/원장 84)**으로 부분 체결·중복·취소 경합·지연/역순 체결,
응답 유실·재시작·미커밋 강제 종료·잘못된 snapshot·원장 변조 탐지·가격/기간/재시도·
정산·현물 비용·t/W/D/latest 정책·호가 무효화·snapshot/manifest/백업을 검증한다.
[검증 JSON](verification/oms-20261104.json)에 독립 wheel 설치·시험 ID·소스/패키지 SHA·
수작업 표·CLI/무결성 결과를 기록한다. 실제 HFT 처리량·브로커 정합성·수익성 시험은 아니다.

다음 #8에서는 이 코어에 명시적 모의 집행 adapter를 붙이고 지연·유동성·비용을 모델링한다.
#9에서 독립 재생·주문 장애·위험 한도를 넓게 검증한다. 실제 전송 전에는 #33의 현장
검증과 실제 계좌/원장/증거금·timestamp/sequence/cancellation의 provider 계약을 완성해야 한다.
