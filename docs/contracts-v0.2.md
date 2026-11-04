# 다중 상품 메시지 계약 v0.2

계약 버전: **0.2.0** · 갱신: 2026-11-04 (Asia/Seoul) · 관련 작업: [#2](https://github.com/TheMostEvident/quant-trading-system/issues/2)

사용자 목표는 국내 주식 현물·국내 선물·해외 선물·크립토 현물이며 첫 검증은 국내 선물이다. 아래 JSON은 모두 합성 자료이다. 실제 선물 코드·승수·증거금·수수료나 실제 API 응답을 의미하지 않는다.

## 1. 버전과 메시지 구성

v0.1의 현물 전용 단위·비용·계좌 가정을 바꾸므로 0.2.0으로 개정한다. 이전 계약은 [v0.1 이력](contracts-v0.1.md)에 보존한다.

| 메시지 | 생산자 → 소비자 | 책임 |
| --- | --- | --- |
| MarketSnapshot | Layer 1 → 신호·집행·향후 Layer 2 | 상품별 관측·특징·동기화 상태 |
| ExecutionReport | 모의 집행/브로커 어댑터 → 원장 | 접수·체결·취소·정정 |
| PositionState | 원장 → 집행·위험·향후 Layer 2 | 계약/기준자산별 포지션·예약 |
| EventPacket | Layer 3 → 기록·향후 Layer 2 | 사건·근거·기초자산/상품 영향 |
| UniverseSnapshot | seed/Layer 0 → 구독·정책 | 후보 계약·자산과 규칙 버전 |
| TradingPolicy | 고정 공급자/Layer 2 → Layer 1 | 목표 총 포지션·조건·제약 |
| AccountState | 상품별 원장·계좌 대조 → 공통 위험 검사 | 자산별 현금·잔고·증거금·계좌 평가 |

AccountState를 명시적으로 추가한다. 같은 계좌 상태를 여러 포지션에 복제해 계좌 자산·증거금을 중복 합산하지 않는다.

## 2. 공통 봉투·시간·표현

| 필드 | 형식·규칙 |
| --- | --- |
| schema_version, message_type | 0.2.0과 위 7종 중 하나 |
| message_id, run_id | 논리 메시지 ID·실행 ID. 재전송에는 같은 ID, 수정은 새 ID |
| instrument_id | 안정적인 상품 ID 또는 복수 자산/계좌 메시지에서 null |
| source, source_event_id | 생산자·실제 원천 ID(null 가능); 거래소 sequence를 임의 생성하지 않음 |
| event_at | 알려진 사건 UTC 시각 또는 null |
| received_at, ready_at | 최초 수집·이 버전의 처리 완료 시각 |
| data_origin | SYNTHETIC / OBSERVED / DERIVED; 합성 파생은 SYNTHETIC 유지 |
| quality_flags, lineage | 품질·시점/누락/미보정 표시와 원천·선행 메시지 참조 |
| payload | 유형별 내용 |

모든 시각은 마이크로초를 표시한 RFC 3339 UTC이다. 원천 정밀도·원래 시각·표시 시간대는 raw 기록에 보존한다. 날짜만 아는 문서에 임의 초 시각을 만들지 않는다. 기간은 정수 ms, 가격·수량·금액·승수는 유한한 10진 문자열이다. 확률·OBI 등 특징은 유한한 JSON 수이다.

자산 코드는 명세의 asset_registry_ref로 식별한다. KRW/USD 같은 법정통화와 BTC/USDT 같은 자산을 구별하며 USDT=USD로 자동 환산하지 않는다. 원화와 외화·토큰 금액은 시점이 있는 평가/환산 기록 없이 합산하지 않는다.

- 같은 시계 기준에서 received_at <= ready_at이다. 소비자는 consumer_seen_at을 추가 기록하고 max(ready_at, consumer_seen_at) 이전에 사용하지 않는다.
- 타임아웃·경과시간은 원래 monotonic 수신 간격을 사용한다. UTC는 감사·소스 간 정렬을 위한 시각이다.
- 재생은 당시 정보 이용 가능 순서·ingest sequence의 가상 시계를 사용한다. 현재 호스트 시각을 과거 정책 만료에 섞지 않는다.
- 중복·역순·누락·정정은 원천과 함께 보존한다. 같은 ID의 다른 내용은 충돌이다. 후속 정정과 지금 분석한 결과의 ready_at을 과거로 되돌리지 않는다.
- source event time과 거래일(trading_date)을 구별한다. 야간 세션·해외 세션·서머타임·크립토의 날짜 경계는 상품별 calendar_ref로 처리한다.
- 가격 부호·허용 범위·호가단위는 상품 규칙에 따른다. 모든 해외 선물 가격에 현물의 양수 조건을 강제하지 않는다. bps의 분모가 0에 가깝거나 의미가 없으면 tick/가격 단위 제한을 사용한다.

## 3. InstrumentSpec과 상품별 장부

InstrumentSpec은 seed 또는 Layer 0의 버전 있는 참조 자료이다. 미확인 capability는 신규 노출 증가를 허용하지 않는다.

| 필드 | 의미 |
| --- | --- |
| spec_id, instrument_id, venue, symbol | 명세·상품·거래소·공급자 심볼. 월물은 별도 상품 ID |
| asset_class | EQUITY / FUTURE / CRYPTO_SPOT |
| underlying_id, base_asset, quote_asset, pnl_asset | 기초자산·기준자산(null 가능)·표시/결제 자산·손익 자산 |
| asset_registry_ref, price_unit | 자산 정의와 가격 단위. 지수 포인트와 원화 가격을 구별 |
| quantity_unit, qty_step, min_qty, max_qty | SHARE / CONTRACT / BASE_ASSET; 상품별 최소·증가 수량 |
| price_rule_ref, notional_rule_ref | 호가·가격 범위·주문 유형별 최소/최대 명목가치 규칙 |
| pnl_model, contract_multiplier | 검증한 손익 구조·선형 계약의 승수. 단위는 손익자산/가격단위/계약 |
| margin_rule_ref, settlement_rule_ref | 증거금·정산 규칙. 현물은 해당 없음, 선물 미확인은 실행 불가 |
| expiry_at, last_trade_at, roll_rule_ref | 만기·최종 거래·롤 규칙; 시각 정밀도와 달력 보존 |
| calendar_ref, session_timezone | 거래일·세션·휴장/중단·IANA 시간대 |
| can_open_long, can_open_short, position_mode | 확인된 기능과 NET 등 포지션 의미 |
| effective_at, expires_at, provenance | 적용 구간·출처와 실제 이용 가능 시각 |

초기 국내 선물은 정수 CONTRACT와 NET 포지션을 검증한다. 반대 side의 BUY/SELL을 무조건 신규·청산으로 추정하지 않고 보유 상태·position_effect·브로커 규칙으로 해석한다. 선형 이외 손익 구조·다른 포지션 모드는 별도 지원 검증 없이 선형 식에 넣지 않는다.

| 상품 | 장부 원칙 |
| --- | --- |
| 주식 현물 | 매수대금·비용과 주식 수량 반영, 보유분 범위 매도 |
| 선형 선물 | 총 명목 노출 = abs(수량 × 가격 × 승수); 현금 전액 매입으로 처리하지 않음 |
| 선형 선물 손익 | 미정산 손익 = signed 수량 × (평가가 - 현재 손익기준가) × 승수 |
| 증거금 | 위험/주문 가능성 계산의 잠금·예약과 실제 비용을 구별; 이론상 고정 비율로 추측하지 않음 |
| 정산 | 확인된 정산 현금흐름과 손익기준가 변경을 함께 기록; 이미 현금 반영한 손익을 미실현에 재가산하지 않음 |
| 만기·롤 | 구월물 종료와 신월물 진입을 별도 주문·가격·비용으로 기록; 연속선물은 분석 계열로만 구별 |
| 크립토 현물 | 매매 gross 수량과 실제 기준/호가/수수료 자산 증감을 구별; 기본 숏·차입 없음 |

수수료·세금·리베이트는 charges 배열의 kind·asset·amount로 보존한다. amount는 자산 감소 비용이 양수, 환급/리베이트가 음수이다. 자산별 원장 반영과 평가용 환산 비용을 두 번 차감하지 않는다.

## 4. MarketSnapshot

| payload | 형식·의미 |
| --- | --- |
| instrument_spec_ref, trading_date | 유효 상품 명세·해당 시장의 거래일 |
| update_kind, market_status | BOOK/TRADE/STATUS; CONTINUOUS/AUCTION/HALTED/CLOSED/UNKNOWN |
| book | coverage·sync_status·source_sequence·bids/asks 또는 null |
| trade | trade_id·price·quantity·aggressor(BUY/SELL/UNKNOWN) 또는 null |
| features | feature_version·time_basis·basis·OBI/EMA/기울기/관측 OFI 또는 null |

BOOK는 어댑터가 정상 동기화한 상위 N단계 스냅숏이다. 개별 source update를 보존하고 실제 보장 가능한 관측 깊이만 표시한다. Binance diff-depth는 공식 스냅숏/업데이트 ID 동기화 절차로 재구성하고 누락 시 재동기화한다. 키움 피드에 같은 sequence 체계가 있다고 가정하지 않는다.

bids는 내림차순, asks는 오름차순이다. 유효 best bid < best ask이며 활성 호가 수량은 양수이다. 삭제(수량 0) 원천은 보존하되 활성 호가에서 제거한다. TRADE/STATUS에 과거 book을 새 호가처럼 복사하지 않는다. stale·gap·동기화 미완료 관측으로 신규 진입하지 않는다.

OBI와 OFI는 같은 상품의 동일 수량 단위 안에서 계산한다. 순수 취소 흐름 등 피드가 식별하지 못하는 부분을 완전 관측으로 표시하지 않는다. 초기 time_basis는 RECEIVE_MONOTONIC이며 유효 관측만 한 번 세고, 첫 기울기/OFI 등 계산 불가능 값은 null이다.

```json
{
  "schema_version":"0.2.0","message_type":"MarketSnapshot","message_id":"m-fut-101","run_id":"demo-fut-20261104",
  "instrument_id":"SIM:KR-FUT1","source":"future-fixture","source_event_id":"book-101",
  "event_at":"2026-11-04T00:00:01.000000Z","received_at":"2026-11-04T00:00:01.008000Z","ready_at":"2026-11-04T00:00:01.010000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["raw-book-101"],
  "payload":{"instrument_spec_ref":"spec-fut-v1","trading_date":"2026-11-04","update_kind":"BOOK","market_status":"CONTINUOUS",
    "book":{"coverage":"TOP_N_SNAPSHOT","sync_status":"SYNCED","source_sequence":null,
      "bids":[{"price":"100.9","quantity":"8"}],"asks":[{"price":"101.0","quantity":"2"}]},
    "trade":null,"features":{"feature_version":"fut-obi-v1","time_basis":"RECEIVE_MONOTONIC","basis":["raw-book-101"],
      "obi":0.6,"smooth_obi":0.6,"obi_slope_per_second":null,"ofi_top1_delta":null,"ofi_window_sum":null,"ofi_window_ms":1000}}
}
```

### 구현된 feature extension: microstructure-v1

봉투와 필수 MarketSnapshot 구조는 0.2.0을 유지한다. `features.feature_version="microstructure-v1"`이면 기존 9개 특징 필드에 `config_ref`와 `details`를 추가한다. 기존 합성 `fut-obi-v1` 예시는 이력으로 유지하며 새 구현의 소비자는 지원 feature_version을 확인해야 한다.

- `time_basis=RECEIVE_MONOTONIC`; details.clock에 producer/epoch/sequence/monotonic_ns/raw_ref를 보존한다.
- `obi_slope_per_second`는 **평활 OBI**의 시간 차분이다. raw 차분과 관측당 차분은 details에 별도로 기록한다. 첫 관측·0시간 간격에서는 시간 변화율이 null이다.
- OFI는 signed CONTRACT 정수다. snapshot 사이의 관측 OFI이며 중간에 보이지 않은 주문·취소를 복원하지 않는다.
- config_ref는 고정된 깊이·가중치·평활화·시간창·상태 초기화 조건의 불변 artifact 참조다. basis는 현재 raw와 이전 계산 입력 메시지를 가리킨다.
- details에 단위·범위·가중치·깊이/스프레드/중간값·관측 수·체결량 BUY/SELL/UNKNOWN·시간창 coverage/overflow를 기록한다. 가격/깊이는 decimal string, OBI/변화율은 유한 JSON number다.
- TRADE에는 새 OBI·book 깊이/스프레드를 복사하지 않는다. STATUS와 부적격 입력의 features는 null이다. 현재 구현은 CONTRACT 수량만 지원한다.

정확한 정의·필드 의미·예시·범위는 [미시구조 지표 안내](microstructure-features.md), 검증은 `contracts/microstructure.py`를 따른다. feature 계산 가능 여부와 quote 입력 gate·실제 주문 가능성은 구별한다.

## 5. ExecutionReport

주문 의도·접수와 실제 체결을 구별한다. 초기 모의는 선물의 신규·청산·양방향 체결 의미를 검증한다.

| payload | 형식·의미 |
| --- | --- |
| account_id, client_order_id, broker_order_id | 계좌 별칭·로컬 주문·브로커 주문(null 가능) |
| event_type, order_status | ACK/REJECT/FILL/CANCEL_ACK/CANCEL_REJECT/REPLACE_ACK/EXPIRE/RECONCILE/CORRECTION과 구체 상태 |
| side, position_effect | BUY/SELL; OPEN/CLOSE/UNKNOWN, 원천 의미 보존 |
| order_qty, cum_filled_qty, leaves_qty | gross 주문·누적 체결·살아 있는 잔량; quantity_unit은 InstrumentSpec |
| last_fill | fill_id·quantity·price·charges, 체결 외에는 null |
| correction_of, reason | 정정 대상·사유 또는 null |

0 <= cum_filled_qty <= order_qty, 0 <= leaves_qty <= order_qty - cum_filled_qty이다. 취소 잔량은 체결이 아니다. fill_id를 한 번만 원장에 반영하고 취소 중/취소 후 도착한 실제 지연 체결도 누락하지 않는다. 역순 누적 수량으로 기존 체결을 되돌리지 않는다. 정정·체결 취소는 원 사건을 참조한 반대 분개와 대조 절차로 처리한다.

```json
{
  "schema_version":"0.2.0","message_type":"ExecutionReport","message_id":"e-fut-1","run_id":"demo-fut-20261104",
  "instrument_id":"SIM:KR-FUT1","source":"internal-paper-future","source_event_id":"fill-1",
  "event_at":"2026-11-04T00:00:01.300000Z","received_at":"2026-11-04T00:00:01.311000Z","ready_at":"2026-11-04T00:00:01.312000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["intent-1","raw-book-104"],
  "payload":{"account_id":"paper-fut","client_order_id":"order-1","broker_order_id":"paper-order-1",
    "event_type":"FILL","order_status":"FILLED","side":"BUY","position_effect":"OPEN","order_qty":"2","cum_filled_qty":"2","leaves_qty":"0",
    "last_fill":{"fill_id":"fill-1","quantity":"2","price":"101.0","charges":[{"kind":"FEE","asset":"KRW","amount":"40"}]},
    "correction_of":null,"reason":null}
}
```

## 6. AccountState

현재 계좌·자산·증거금의 권위 있는 단일 버전이다. 포지션·정책의 account_state_ref가 이 메시지를 참조한다.

| payload | 형식·의미 |
| --- | --- |
| account_id, account_version, ledger_model | 계좌·증가 버전·CASH/FUTURES_MARGIN 등 검증한 장부 |
| balances | 자산별 wallet/free/locked; 지원 자산마다 단위·소유 의미 유지 |
| margin | initial_used·maintenance_required·order_reserved·available_margin·asset·rule_ref·status, 현물은 null |
| valuation | equity·asset·as_of·model_ref·fx_refs; 평가·환산 근거 |
| unsettled_pnl | 자산별 아직 현금에 반영하지 않은 손익 |
| adjustments | 입출금·정산·비용 등 분개 ID 참조. 같은 ID는 한 번만 반영 |
| reconciliation | INTERNAL_ONLY/MATCHED/UNRECONCILED/MISMATCH |

계좌 현금과 가용 증거금은 같은 값이 아니다. 평가자산과 담보 인정·가용 계산은 해당 규칙에 따르며 현물 잔고·선물 증거금을 한 식으로 처리하지 않는다. 여러 통화·자산의 평가에 필요한 시점별 환산이 없으면 통합 평가를 미확정으로 표시한다.

아래는 단순 선형 선물의 **합성 모의**이다. 초기 현금 1,000,000, 수수료 40, 계약당 증거금 20,000, 매수 2계약·101.0, 승수 1,000, 평가가 102.0을 가정한다. 현금은 999,960, 미정산 손익은 2,000, 평가자산은 1,001,960이다. 증거금 40,000은 추가 비용이 아니다.

```json
{
  "schema_version":"0.2.0","message_type":"AccountState","message_id":"a-fut-2","run_id":"demo-fut-20261104",
  "instrument_id":null,"source":"paper-futures-ledger","source_event_id":null,
  "event_at":"2026-11-04T00:00:01.400000Z","received_at":"2026-11-04T00:00:01.410000Z","ready_at":"2026-11-04T00:00:01.414000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["e-fut-1","raw-book-105"],
  "payload":{"account_id":"paper-fut","account_version":2,"ledger_model":"FUTURES_MARGIN",
    "balances":[{"asset":"KRW","wallet":"999960","free":"959960","locked":"40000"}],
    "margin":{"asset":"KRW","initial_used":"40000","maintenance_required":"30000","order_reserved":"0","available_margin":"961960",
      "rule_ref":"synthetic-margin-v1","status":"NORMAL"},
    "valuation":{"equity":"1001960","asset":"KRW","as_of":"2026-11-04T00:00:01.400000Z","model_ref":"synthetic-linear-v1","fx_refs":[]},
    "unsettled_pnl":[{"asset":"KRW","amount":"2000"}],"adjustments":["charge-fill-1"],"reconciliation":"INTERNAL_ONLY"}
}
```

실제 계좌의 가용 금액·증거금 계산이 이 예와 같다고 가정하지 않는다. 정산으로 2,000이 현금에 반영되면 그에 맞춰 미정산 손익을 0으로 하고 손익기준가를 갱신해야 같은 평가자산이 유지된다.

## 7. PositionState

| payload | 형식·의미 |
| --- | --- |
| account_id, account_state_ref, position_version | 계좌·유효 계좌 버전·상품 원장 증가 버전 |
| instrument_spec_ref, position_status, position_mode | 상품·미보유/주문 중/부분체결/보유/청산 중/불명/중단·NET 등 |
| net_qty, quantity_unit | signed 계약/주/기준자산 수량. 현물은 기본적으로 음수 불허 |
| avg_entry_price, pnl_reference_price, mark_price | 평균 체결가·현재 손익기준가·평가가; 정산 후 기준가를 구별 |
| contract_multiplier, pnl_asset | 선형 승수·손익 자산 |
| reserved_buy_qty, reserved_sell_qty, active_orders | 실제 보유와 별도 주문 예약·주문 ID/잔량/상태 |
| unrealized_pnl_gross, charges_paid, settlement_ref | 미정산 평가손익·자산별 비용 이력·정산 근거 |
| reconciliation | 대조 상태; 불명 상태가 실제 보유 수량을 0으로 만들지 않음 |

선물에서는 숏이 음수 net_qty이다. 목표 부호가 반대로 바뀌면 초기 집행 규칙은 기존 수량 청산 확인 후 신규 노출을 검토한다. 미체결·취소 경합을 무시한 수량 역전을 허용하지 않는다. 현물의 예약 매도는 실제 처분 가능 보유분을 초과할 수 없다.

```json
{
  "schema_version":"0.2.0","message_type":"PositionState","message_id":"p-fut-2","run_id":"demo-fut-20261104",
  "instrument_id":"SIM:KR-FUT1","source":"paper-futures-ledger","source_event_id":null,
  "event_at":"2026-11-04T00:00:01.400000Z","received_at":"2026-11-04T00:00:01.410000Z","ready_at":"2026-11-04T00:00:01.415000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["e-fut-1","a-fut-2","raw-book-105"],
  "payload":{"account_id":"paper-fut","account_state_ref":"a-fut-2","position_version":2,"instrument_spec_ref":"spec-fut-v1",
    "position_status":"OPEN","position_mode":"NET","net_qty":"2","quantity_unit":"CONTRACT",
    "avg_entry_price":"101.0","pnl_reference_price":"101.0","mark_price":"102.0","contract_multiplier":"1000","pnl_asset":"KRW",
    "reserved_buy_qty":"0","reserved_sell_qty":"0","active_orders":[],"unrealized_pnl_gross":"2000",
    "charges_paid":[{"kind":"FEE","asset":"KRW","amount":"40"}],"settlement_ref":"initial-basis-v1","reconciliation":"INTERNAL_ONLY"}
}
```

계좌 평가자산은 AccountState의 장부·평가 근거로 계산한다. 이미 현금에 반영한 비용이나 정산금에 PositionState의 누계 비용·손익을 다시 가감하지 않는다.

## 8. EventPacket

| payload | 형식·의미 |
| --- | --- |
| event_id, event_revision, event_type, status | 사건·수정 버전·유형·ACTIVE/CORRECTED/RETRACTED/UNKNOWN |
| document_ids, evidence_refs | 원문·문장/문단/표·페이지 근거 |
| published_at, published_date, publication_precision | 알려진 발행 시각/일자와 정밀도 |
| affected_targets | instrument_id 또는 underlying_id/asset_id에 대한 관련성·경제적 방향·중요도 |
| model_id, prompt_version, raw_response_ref | 실제 모델·질문·응답 이력. fixture는 명시 |
| supersedes, expires_at | 이전 버전·의사결정 입력의 사용 종료 |

회사·업종·거시 뉴스가 선물 계약이나 크립토에 미치는 연결을 버전 있는 기초자산/구성·관계 자료로 남긴다. 기업 이름이 나오면 모든 관련 선물에 같은 방향을 부여하지 않는다. Noul 명제·Choice 전체 분포·Score rubric은 별도 보존하며 가격점프 확률로 직접 치환하지 않는다.

```json
{
  "schema_version":"0.2.0","message_type":"EventPacket","message_id":"ev-message-1","run_id":"demo-fut-20261104",
  "instrument_id":null,"source":"event-fixture","source_event_id":"doc-1","event_at":null,
  "received_at":"2026-11-04T00:00:00.300000Z","ready_at":"2026-11-04T00:00:00.900000Z",
  "data_origin":"SYNTHETIC","quality_flags":["SOURCE_TIME_UNKNOWN","UNVALIDATED_MODEL"],"lineage":["doc-1"],
  "payload":{"event_id":"event-1","event_revision":1,"event_type":"MACRO_ANNOUNCEMENT","status":"ACTIVE",
    "document_ids":["doc-1"],"evidence_refs":["doc-1:block-3"],"published_at":null,"published_date":"2026-11-04","publication_precision":"DATE",
    "affected_targets":[{"instrument_id":"SIM:KR-FUT1","underlying_id":"SIM:INDEX1","mapping_ref":"underlying-map-v1",
      "relevance_noul":{"statement":"문서가 해당 기초지수와 연결된 거시 발표를 명시하는가","value":0.8},
      "direction_probabilities":{"POSITIVE":0.7,"NEGATIVE":0.1,"MIXED":0.05,"UNCLEAR":0.15},
      "importance":{"rubric_version":"importance-demo-v1","score":1.0,"legend":["LOW","MEDIUM","HIGH"]}}],
    "model_id":"fixture-not-a-jev-call","prompt_version":"event-demo-v1","raw_response_ref":"response-fixture-1","supersedes":null,
    "expires_at":"2026-11-04T00:10:00.000000Z"}
}
```

## 9. UniverseSnapshot

| payload | 형식·의미 |
| --- | --- |
| universe_id, revision, source_kind | ID·버전·SEED/LAYER0 |
| as_of, valid_from, expires_at | 계산 기준과 적용 구간 [valid_from, expires_at) |
| members | 상품·명세·기초자산 매핑·선정 이유·배경 특징 참조 |
| excluded, coverage, rule_version | 제외 이유·실제 분석 범위·규칙 버전 |

모든 계약은 만기·거래 상태·자료 신선도를 만족해야 한다. 구월물 제외가 기존 포지션/주문의 위험 관리 구독을 끊지 않도록 한다. 연속선물 분석 심볼을 실제 주문 계약 ID로 사용하지 않는다.

```json
{
  "schema_version":"0.2.0","message_type":"UniverseSnapshot","message_id":"u-fut-1","run_id":"demo-fut-20261104",
  "instrument_id":null,"source":"manual-seed","source_event_id":null,"event_at":null,
  "received_at":"2026-11-03T23:59:00.000000Z","ready_at":"2026-11-03T23:59:00.010000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["spec-fut-v1","underlying-map-v1"],
  "payload":{"universe_id":"seed-fut","revision":1,"source_kind":"SEED","as_of":"2026-11-03T23:59:00.000000Z",
    "valid_from":"2026-11-03T23:59:00.010000Z","expires_at":"2026-11-04T06:30:00.000000Z",
    "members":[{"instrument_id":"SIM:KR-FUT1","instrument_spec_ref":"spec-fut-v1","underlying_ref":"underlying-map-v1",
      "reason_codes":["SYNTHETIC_DOMESTIC_FUTURE"],"background_ref":null}],
    "excluded":[],"coverage":"ONE_SYNTHETIC_FUTURE","rule_version":"seed-fut-v1"}
}
```

## 10. TradingPolicy

| payload | 형식·의미 |
| --- | --- |
| policy_id, provider_kind, provider_epoch, revision | ID·STATIC/LAYER2·재시작 구분·계좌/상품별 증가 버전 |
| account_id, instrument_spec_ref, universe_ref | 계좌·상품·후보군 |
| valid_from, expires_at | 적용 구간; 소비자에게 실제 도착한 시각도 충족 |
| allowed_position_directions, target_position_qty | LONG/SHORT 및 signed **목표 총 수량**; 추가 주문 수량 아님 |
| entry, hold_exit, smoothing | 진입·유지·청산 조건·최대 보유시간·평활화 기준 |
| execution_limits | 최대 주문 명목가치·수량·초기 증거금·호가/시간·가격 추격·재시도 제약 |
| risk_budget_ref, regime, rationale, input_refs | 계좌 전체 한도·모형 불확실성·근거·유효 입력 |

정책 목표만으로 주문하지 않는다. 실제 net_qty·미체결 예약·가용 증거금/자산 잔고·상품 capability를 함께 검사한다. 선물은 abs(target_qty)와 명목가치·증거금 한도를 따로 검사한다. 크립토 현물의 목표는 BASE_ASSET 수량이며 quote 금액이나 계약 수로 혼동하지 않는다.

정책은 원자적으로 교체하고 생산자 epoch/revision 및 도착 기록으로 중복·역순을 처리한다. 만료 후 신규 진입은 제한하되 기존 주문·포지션의 위험 관리 규칙은 유지한다. max_hold_ms는 최초 체결부터 계산하며 재주문·정책 갱신으로 초기화하지 않는다.

entry의 N회·경과시간·최대 간격·EMA·OBI 히스테리시스는 독립 조건이다. 중복 또는 계산 불가 관측을 지속성 증거로 세지 않는다. 롱/숏의 유지·청산 문턱은 별도이며 OBI 변화율 부호만으로 전량 청산하지 않는다.

price_reference_mode는 AT_INTENT_CREATION 또는 FIXED_REFERENCE이다. 실제 주문 의도에 고정한 유효 호가 ID로 가격 상·하한을 계산하고 재시도로 기준을 움직여 추격 한도를 무력화하지 않는다. 가격이 0 근처인 등 bps가 부적절하면 tick/가격 단위 규칙으로 제한한다.

```json
{
  "schema_version":"0.2.0","message_type":"TradingPolicy","message_id":"tp-fut-1","run_id":"demo-fut-20261104",
  "instrument_id":"SIM:KR-FUT1","source":"static-policy","source_event_id":null,"event_at":null,
  "received_at":"2026-11-04T00:00:00.000000Z","ready_at":"2026-11-04T00:00:00.010000Z",
  "data_origin":"SYNTHETIC","quality_flags":[],"lineage":["u-fut-1"],
  "payload":{"policy_id":"policy-fut-1","provider_kind":"STATIC","provider_epoch":"demo-epoch-1","revision":1,
    "account_id":"paper-fut","instrument_spec_ref":"spec-fut-v1","universe_ref":"u-fut-1",
    "valid_from":"2026-11-04T00:00:00.010000Z","expires_at":"2026-11-04T00:01:00.000000Z",
    "allowed_position_directions":["LONG","SHORT"],"target_position_qty":"2",
    "entry":{"obi_long":0.6,"obi_short":-0.6,"slope_min_per_second":null,"min_observations":3,"min_duration_ms":200,"max_observation_gap_ms":500},
    "hold_exit":{"long_hold_obi":0.2,"short_hold_obi":-0.2,"max_hold_ms":30000,"exit_target_qty":"0","emergency_rule_ref":"futures-emergency-demo-v1"},
    "smoothing":{"method":"TIME_EMA","tau_ms":500},
    "execution_limits":{"max_order_notional":{"asset":"KRW","amount":"300000"},"max_position_qty":"2",
      "max_initial_margin":{"asset":"KRW","amount":"50000"},"max_spread_ticks":2,"max_quote_age_ms":1000,
      "order_ttl_ms":1000,"max_retries":0,"max_slippage_ticks":1,"price_reference_mode":"AT_INTENT_CREATION","price_reference_ref":null},
    "risk_budget_ref":"risk-fut-v1","regime":null,"rationale":"PLUMBING_TEST_ONLY","input_refs":["u-fut-1"]}
}
```

예시의 수량·호가·기간·증거금은 최적 정책·실제 증권사 조건이 아니다. 재시도·비상 청산은 미체결·취소·잔여 수량을 먼저 확인하며 시장가의 즉시 체결을 보장하지 않는다.

## 11. 필수 참조 fixture와 검증 사례

위 JSON은 다음 자료가 #3 이후 실제 fixture로 작성될 것을 요구하는 설계 예시이다. 참조 객체가 이미 저장·구현되었다는 의미가 아니다.

| 참조 | 합성 입력 |
| --- | --- |
| spec-fut-v1 | SIM:KR-FUT1, FUTURE, CONTRACT 정수, NET, price_unit=POINT, pnl_asset=KRW, 승수 1000, tick=0.1, 양방향 허용 |
| synthetic-margin-v1 | 계약당 초기 20000·유지 15000의 단순 모의, 계좌 평가자산 기준 가용 계산; 실제 조건 아님 |
| 초기 계좌·risk-fut-v1 | 현금 1000000, 초기 보유·미체결 0, 모의 위험 한도와 실행 모드 |
| 달력·만기·정산 seed | 합성 거래일·세션, 유효 만기/최종 거래 시각, 가격·손익 기준 및 정산 이벤트 규칙 |
| raw-book-101…105 / intent-1 | 1.000/1.100/1.200Z의 지속 관측 → 주문 의도 → 지연 조건 충족 1.300Z 체결 → 1.400Z 평가 |
| underlying-map-v1 / doc-1 | 합성 기초지수와 문서 근거·수동 평가 응답 |
| futures-emergency-demo-v1 | 신규 진입 차단·주문/계좌 대조·잔여 계약 수량 관리 |

| 검증 사례 | 기대 결과 |
| --- | --- |
| 선물 롱 2·기준가 101·평가 102·승수 1000 | 미정산 +2000 |
| 선물 숏 -2·기준가 101·평가 100·승수 1000 | 미정산 +2000 |
| 위 롱 예시의 현금·수수료·증거금 | 현금 999960, equity 1001960, margin used 40000; 명목가치 202000 전액 차감 금지 |
| 정산으로 미정산 2000을 현금 반영 | 현금 1001960·미정산 0·기준가 갱신; equity 유지 |
| 크립토 현물 0.002 BTC 매수·가격 50000 USDT | gross 매매대금 100 USDT |
| 위 현물 수수료 0.000002 BTC | BTC 순증 0.001998, USDT 감소 100; 0.000002를 USDT 비용으로 잘못 차감하지 않음 |
| 비정상 증거금·유효 규칙 미확인·미동기화 호가 | 신규 노출 증가 차단, 기존 포지션·주문 관리 유지 |
| 만기/후보 제외·취소 중 체결·반대 방향 목표 | 실보유·예약을 보존하고 중복 청산·유령 포지션 방지 |

크립토 수수료가 기준자산·호가자산·제3자산인 경우를 각각 검사한다. lot/price/notional 조건은 실제 거래쌍의 버전 있는 규칙으로 검증한다. 서로 다른 통화·토큰의 잔고나 위험 예산을 숫자만 더하지 않는다.

## 12. 후속 구현과 호환성

- v0.1의 currency → quote_asset/pnl_asset/자산별 charges로 구분한다. 문자열 이름만 바꾸어 새 장부를 지원했다고 간주하지 않는다.
- qty는 현물 주·선물 계약·소수 기준자산의 상품 규칙을 따른다. 초기 국내 선물 검증에서 다른 상품을 즉시 거래하도록 강제하지 않는다.
- 첫 국내 선물의 모의 집행·원장·검증은 #7–9, 배경 상품 자료는 #16–19, 최종 모형·위험 정책은 #24–27에서 구현·검증한다.
- 문서의 JSON·계산·참조 검토와 실제 COM/REST/WebSocket·체결·원장 검증을 분리한다.
