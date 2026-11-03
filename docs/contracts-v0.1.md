> **이력 문서:** 2026-11-04 사용자 선택으로 현물 우선 가정이 대체되었다. 현재 설계 기준은 [계약 v0.2](contracts-v0.2.md)이다. 아래 예시는 이전 현물 전용 제안으로 보존하며 선물·크립토 구현에 그대로 적용하지 않는다.

# Layer 간 메시지 계약 v0.1

계약 버전: **0.1.0** · 상태: 문서 제안 · 관련 작업: [#2](https://github.com/TheMostEvident/quant-trading-system/issues/2)

아래 JSON은 **데이터 형식 예시**이며 실행 코드·실측 시세·Jev 호출 결과가 아니다. 모든 예시는 합성 상품 `SIM:EQ1`을 사용한다. 메시지 구조는 seed와 Layer 0, 고정 정책과 Layer 2를 교체해도 유지한다.

## 1. 공통 봉투와 기본 단위

각 메시지는 다음 필드를 갖는다. 필수 필드는 null 허용이 명시된 경우에만 null을 사용한다.

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| schema_version | 문자열 | `0.1.0`; 알 수 없는 호환 버전은 격리하고 신규 진입 입력으로 사용하지 않음 |
| message_type | 문자열 | 아래 정의한 6개 유형 중 하나 |
| message_id | 문자열 | 같은 논리 메시지의 재전송에는 같은 ID; 같은 ID의 상이한 payload는 충돌 |
| run_id | 문자열 | 입력 출처·정책·모드·설정이 고정된 실행 기록 |
| instrument_id | 문자열 또는 null | 마스터 내부의 안정적인 상품 식별자; 복수 자산 메시지는 null 가능 |
| source | 문자열 | 원천/생산자 식별자; 공급자 필드명과 구별 |
| source_event_id | 문자열 또는 null | 공급자 이벤트 ID; 없으면 null과 로컬 식별 방식을 원천 기록에 보존 |
| event_at | UTC 시각 또는 null | 원천 사건의 알려진 시각; 발행 일자만 알 때 초 단위 시각을 생성하지 않음 |
| received_at | UTC 시각 | 이 입력을 수집 계층에서 최초로 관측한 시각 |
| ready_at | UTC 시각 | 이 버전의 메시지가 생산자 처리 후 사용 가능해진 시각 |
| data_origin | 문자열 | `SYNTHETIC`, `OBSERVED`, `DERIVED`; 재생 여부는 run 기록에서 별도 관리 |
| quality_flags | 문자열 배열 | 예: `SOURCE_TIME_UNKNOWN`, `STALE`, `GAP`, `OUT_OF_ORDER`, `INVALID_BOOK`, `PARTIAL_PARSE`, `UNVALIDATED_MODEL` |
| lineage | 문자열 배열 | 원천 레코드·문서·선행 메시지의 식별자; 조회 가능한 동일 실행/자료 저장소 참조 |
| payload | 객체 | 유형별 내용 |

| 값 | 표현과 단위 |
| --- | --- |
| 시각 | RFC 3339 UTC, 마이크로초 고정 정밀도: `2026-11-03T00:00:01.000000Z` |
| 기간 | 필드명에 `_ms`를 붙인 0 이상의 정수; 변화율은 명시한 초 기준 |
| 가격·금액·수량·승수 | 10진 문자열. 지수 표기·NaN·Infinity 불가; 장부 계산은 10진/고정소수점 의미 유지 |
| 수량 | 현물은 주, 선물은 계약. 상품의 quantity_unit과 lot_size에 따름; 현물 분할 단위를 임의 허용하지 않음 |
| 통화 | ISO 통화 코드; 초기 KRW. 서로 다른 통화를 환산 근거 없이 합산하지 않음 |
| 비율·특징값·확률 | 유한한 JSON 수. OBI는 [-1,1], 확률은 [0,1], bps는 1/10,000 |
| 선택지 확률 | 지정한 전체 선택지에 대한 분포; 합은 수치 허용오차 1e-6 내에서 1 |

### 시간·중복·재생 규칙

- `received_at <= ready_at`은 같은 시계 기준에서 성립해야 한다. 각 소비자는 도착 시각 `consumer_seen_at`도 별도 기록하고 `max(ready_at, consumer_seen_at)` 이전에 메시지를 사용하지 않는다.
- `event_at`이 알려져 있어도 미래에 발행될 자료나 사후 정정을 과거 판단에 사용할 수 없다. 원천의 시계 오차는 quality flag와 원문 시각으로 보존한다.
- 경과시간·타임아웃은 실행 호스트의 monotonic clock으로 측정한다. UTC 시각은 기록·교차 소스 정렬용이다. 호스트 간 시계 차이의 허용 범위는 #3에서 정하고 측정한다.
- 봉투의 최초 관측·처리 시각과 소비자의 로컬 수신 시각을 혼동하지 않는다. 지연 전달된 정책은 ready_at만으로 즉시 유효하다고 처리하지 않는다.
- 재생은 원래 소비자에게 이용 가능해진 순서와 저장된 단조 증가 ingest sequence를 사용한다. 거래소 사건 시각만으로 재정렬해 과거에 없던 정보 순서를 만들지 않는다.
- 재생의 의사결정에는 원래 이용 가능 시각을 복원한 가상 시계를 사용한다. 재생 호스트의 현재 시각·처리 완료 시각은 별도 실행 기록이다. 현재 시각을 과거 정책의 만료 검사에 섞거나, 지금 다시 분석한 문서의 ready_at을 과거로 되돌리지 않는다.
- 최소 한 번 전달을 가정한다. 메시지 ID와 도메인 식별자(체결 ID 등)를 함께 사용해 중복을 막는다. 역순·누락 후 복구 방법은 원천의 지원 기능에 따라 어댑터에서 정한다.
- 정정·수정은 새 message_id와 lineage로 추가한다. 이전 메시지와 당시 처리 결과를 덮어쓰지 않는다.
- 미확인·무효·노후 데이터가 신규 진입을 허용하지 않도록 한다. 기존 포지션의 청산·위험 관리는 신규 진입 차단과 별도로 유지한다.
- 합성 입력에서 파생한 메시지는 SYNTHETIC 상태를 유지한다. DERIVED라는 이름으로 합성 기원을 지우지 않으며, 실제/합성 입력 혼합 여부도 실행 기록에 남긴다.

## 2. 공유 상품 명세: InstrumentSpec

상품 명세는 seed 또는 Layer 0가 제공한다. 아래 값이 현재 시점에 유효하다는 근거가 있어야 거래 가능 기능으로 사용할 수 있다.

| 필드 | 형식·의미 |
| --- | --- |
| spec_id, instrument_id, symbol, venue | 각각 명세 버전 ID·내부 ID·공급자 심볼·시장 식별자 |
| asset_class | `EQUITY` 또는 후속 검토 상품; 초기 집행 어댑터는 EQUITY만 지원 |
| currency, quantity_unit, lot_size | 예: KRW·SHARE·`1` |
| price_rule_ref, calendar_ref | 시점별 호가 규칙·세션 규칙 참조; 단일 고정 틱을 모든 가격대에 적용하지 않음 |
| contract_multiplier | 현물은 `1`; 파생상품은 검증된 승수와 정산/증거금 규칙이 추가되어야 지원 가능 |
| can_open_long, can_open_short | 실제 상품·계좌·자료로 확인된 가능 여부; unknown은 신규 진입 불허 |
| effective_at, expires_at | `[effective_at, expires_at)` 동안 명세 적용; 알려진 이용 가능 시각은 봉투로 별도 보존 |
| provenance | seed의 합성/수동 상태 또는 원천·검증 이력 |

봉투의 instrument_id와 payload의 상품 ID가 일치해야 한다. 심볼 문자열만으로 계좌·상품·거래소를 추측하지 않는다. 선물의 승수만 입력했다고 현물 원장이 선물을 지원하는 것은 아니다.

## 3. MarketSnapshot

생산자: Layer 1의 정규화·특징 경로. 소비자: 신호/집행, 기록·재생, 향후 Layer 2.

| payload 필드 | 형식·의미 |
| --- | --- |
| instrument_spec_ref | 유효 InstrumentSpec 참조 |
| update_kind | `BOOK`, `TRADE`, `STATUS` |
| market_status | `CONTINUOUS`, `AUCTION`, `HALTED`, `CLOSED`, `UNKNOWN` |
| book | null 또는 객체: coverage, bids, asks, source_sequence; 각 호가는 price·quantity |
| trade | null 또는 객체: trade_id, price, quantity, aggressor(`BUY`/`SELL`/`UNKNOWN`) |
| features | null 또는 객체: feature_version, time_basis, basis, obi, smooth_obi, obi_slope_per_second, ofi_top1_delta, ofi_window_sum, ofi_window_ms |

BOOK는 관측 가능한 상위 N단계의 완전 스냅숏으로 정규화한다. `coverage=TOP_N_SNAPSHOT`은 거래소 전체 주문장을 뜻하지 않는다. TRADE/STATUS에서 book은 null이며 이전 호가를 새 호가처럼 복사하지 않는다. 소비자가 최신 호가를 캐시하면 그 호가의 원래 시간과 신선도를 별도로 유지한다.

가격은 양수, 수량은 0 이상이어야 한다. 양쪽 유효 호가가 있는 경우 bids는 가격 내림차순, asks는 오름차순이고 best bid < best ask여야 한다. 무효 호가는 원천에 보존하되 진입용 특징 계산에 사용하지 않는다.

특징의 basis는 해당 시점까지 수신된 유효 관측과 시간창을 참조한다. 첫 관측의 OFI·기울기 등 계산 불가능 값은 null이다. 한 원천 관측을 정규화/특징 단계에서 각각 전달하더라도 지속성 카운트는 한 번만 증가시킨다. N틱은 중복 메시지 수가 아니라 정의한 유효 관측 수이다.

초기 time_basis는 `RECEIVE_MONOTONIC`이며 원래 수신 경과시간을 원천 기록에 보존해 EMA·기울기·지속성을 재생한다. 검증된 사건 시각 기반의 `EVENT_TIME`을 추가할 경우 지연·역순 처리와 시간창을 별도 정의하고 feature_version을 구별한다. 한 특징 계산에서 두 시간 기준을 섞지 않는다.

```json
{
  "schema_version": "0.1.0", "message_type": "MarketSnapshot",
  "message_id": "market-demo-101", "run_id": "demo-20261103",
  "instrument_id": "SIM:EQ1", "source": "fixture-feed", "source_event_id": "book-101",
  "event_at": "2026-11-03T00:00:01.000000Z",
  "received_at": "2026-11-03T00:00:01.008000Z",
  "ready_at": "2026-11-03T00:00:01.010000Z",
  "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": ["raw-book-101"],
  "payload": {
    "instrument_spec_ref": "spec-demo-eq1-v1", "update_kind": "BOOK", "market_status": "CONTINUOUS",
    "book": {"coverage": "TOP_N_SNAPSHOT", "source_sequence": 101,
      "bids": [{"price": "10000", "quantity": "700"}],
      "asks": [{"price": "10010", "quantity": "100"}]},
    "trade": null,
    "features": {"feature_version": "obi-ofi-v0.1", "time_basis": "RECEIVE_MONOTONIC", "basis": ["raw-book-101"],
      "obi": 0.75, "smooth_obi": 0.75, "obi_slope_per_second": null,
      "ofi_top1_delta": null, "ofi_window_sum": null, "ofi_window_ms": 1000}
  }
}
```

features.basis는 계산에 사용한 원천 관측·선행 메시지 참조이다. 해당 특징 메시지 자체를 계산의 선행 입력으로 참조하지 않는다.

## 4. ExecutionReport

생산자: 내부 모의 집행 또는 후속 브로커 어댑터. 소비자: Layer 1 주문 상태·원장. 주문 의도·발송은 ExecutionReport가 아니며 실제 체결 전 보유 수량을 변경하지 않는다.

| payload 필드 | 형식·의미 |
| --- | --- |
| account_id, client_order_id, broker_order_id | 계좌 별칭·로컬 주문 ID·브로커 ID(null 가능) |
| event_type | `ACK`, `REJECT`, `FILL`, `CANCEL_ACK`, `CANCEL_REJECT`, `REPLACE_ACK`, `EXPIRE`, `RECONCILE`, `CORRECTION` |
| order_status | `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`, `UNKNOWN` |
| side, order_qty, cum_filled_qty, leaves_qty | BUY/SELL, 원주문 수량·누적 체결·현재 살아 있는 잔량 |
| last_fill | FILL일 때 fill_id·quantity·price·fee·tax·currency; 그 외 null |
| reason | 실패·정정·대조 사유 또는 null |

FILL은 fill_id로 한 번만 원장에 반영한다. `0 <= cum_filled_qty <= order_qty`이며 `0 <= leaves_qty <= order_qty - cum_filled_qty`이다. 취소·만료의 leaves_qty는 0일 수 있으며 미체결 취소 수량을 체결 수량으로 처리하지 않는다.

역순 통보의 cum_filled_qty가 작다는 이유로 기존 체결을 되돌리지 않는다. 취소 확인 뒤 도착한 실제 지연 체결도 유일한 fill_id와 원주문 수량 범위에서 반영한다. 취소 거절은 원주문 소멸을 뜻하지 않는다. 정정·체결 취소는 별도 CORRECTION·원 체결 참조·대조 절차로 처리하며 단순 값 덮어쓰기를 금한다.

```json
{
  "schema_version": "0.1.0", "message_type": "ExecutionReport",
  "message_id": "exec-demo-1", "run_id": "demo-20261103",
  "instrument_id": "SIM:EQ1", "source": "internal-paper", "source_event_id": "paper-fill-1",
  "event_at": "2026-11-03T00:00:01.300000Z",
  "received_at": "2026-11-03T00:00:01.311000Z",
  "ready_at": "2026-11-03T00:00:01.312000Z",
  "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": ["intent-demo-1", "raw-book-104"],
  "payload": {"account_id": "paper-demo", "client_order_id": "order-demo-1", "broker_order_id": "paper-order-1",
    "event_type": "FILL", "order_status": "FILLED", "side": "BUY",
    "order_qty": "1", "cum_filled_qty": "1", "leaves_qty": "0",
    "last_fill": {"fill_id": "paper-fill-1", "quantity": "1", "price": "10010", "fee": "1", "tax": "0", "currency": "KRW"},
    "reason": null}
}
```

fee·tax는 설명용 합성 금액이다. 현재 수수료·세율을 의미하지 않는다. 체결은 주문 결정 이후의 적격 관측과 설정한 지연을 사용한다.

## 5. PositionState

생산자: Layer 1의 체결 원장·대조 경로. 소비자: 집행·위험 관리·향후 Layer 2.

| payload 필드 | 형식·의미 |
| --- | --- |
| account_id, position_version | 계좌 별칭과 해당 상품 원장의 단조 증가 버전 |
| position_status | `FLAT`, `ENTRY_PENDING`, `PARTIALLY_FILLED`, `OPEN`, `EXIT_PENDING`, `UNKNOWN`, `HALTED`; 주문의 구체 상태는 active_orders에 별도 보존 |
| net_qty, avg_entry_price, quantity_unit, currency | signed 보유 수량·비용 전 평균 체결가(null 가능)·단위·통화 |
| reserved_buy_qty, reserved_sell_qty, reserved_cash | 살아 있는 주문에 예약한 수량·현금; 실제 포지션과 구분 |
| active_orders | 주문 ID·side·잔량·상태·가격 제한·유효기간 목록 |
| account_state_ref | 해당 시점의 권위 있는 계좌 전체 현금·위험예산·노출 상태 참조 |
| mark_price, mark_source_ref | 평가 가격과 원천 참조(null 가능); 오래된 평가를 새 평가로 표시하지 않음 |
| realized_pnl_gross, unrealized_pnl_gross, fees_paid, taxes_paid | 각각 비용 전 손익과 별도 비용 누계 |
| reconciliation | `INTERNAL_ONLY`, `MATCHED`, `UNRECONCILED`, `MISMATCH`; 내부 모의에 한해 INTERNAL_ONLY 사용 |

기본 키는 (run_id, account_id, instrument_id)이다. net_qty는 실제 체결과 확인된 대조 결과로만 변경한다. v0.1 현물에서는 net_qty >= 0, reserved_sell_qty <= net_qty이며 예약 매수까지 포함해 수량·현금·계좌 전체 한도를 검사한다.

계좌 전체 상태는 account_state_ref의 단일 버전으로 계산한다. 여러 상품의 PositionState에 같은 계좌가 등장해도 현금·위험예산을 중복 합산하지 않는다. 참조를 조회할 수 없거나 계좌 대조가 불명확하면 신규 노출 증가를 제한한다. HALTED/UNKNOWN은 실제 보유 수량을 0으로 만드는 상태가 아니다.

```json
{
  "schema_version": "0.1.0", "message_type": "PositionState",
  "message_id": "position-demo-1", "run_id": "demo-20261103",
  "instrument_id": "SIM:EQ1", "source": "paper-ledger", "source_event_id": null,
  "event_at": "2026-11-03T00:00:01.300000Z",
  "received_at": "2026-11-03T00:00:01.311000Z",
  "ready_at": "2026-11-03T00:00:01.313000Z",
  "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": ["exec-demo-1"],
  "payload": {"account_id": "paper-demo", "position_version": 1, "position_status": "OPEN",
    "net_qty": "1", "avg_entry_price": "10010", "quantity_unit": "SHARE", "currency": "KRW",
    "reserved_buy_qty": "0", "reserved_sell_qty": "0", "reserved_cash": "0", "active_orders": [],
    "account_state_ref": "account-demo-v2", "mark_price": "10000", "mark_source_ref": "raw-book-104",
    "realized_pnl_gross": "0", "unrealized_pnl_gross": "-10", "fees_paid": "1", "taxes_paid": "0",
    "reconciliation": "INTERNAL_ONLY"}
}
```

합성 장부 예: 시작 현금 1,000,000원에서 10,010원 매수와 수수료 1원을 반영하면 현금은 989,989원이다. bid 10,000원 평가 시 자산은 999,989원이고 손익은 -11원이다. 평가손익 -10원과 비용 1원을 중복 차감하지 않는다.

## 6. EventPacket

생산자: Layer 3. 소비자: 이벤트 저장·검토·향후 Layer 2. 초기 Layer 1은 EventPacket을 직접 주문으로 변환하지 않는다.

| payload 필드 | 형식·의미 |
| --- | --- |
| event_id, event_revision, event_type | 사건 ID·단조 증가 버전·정의한 사건 유형 |
| document_ids, evidence_refs | 관련 원문 ID·페이지/블록/문장/표 위치; 저장소에서 조회 가능 |
| published_at, published_date, publication_precision | 정확한 발행 시각 또는 null, 알려진 일자 또는 null, `SECOND`/`DATE`/`UNKNOWN` |
| status, supersedes | `ACTIVE`, `CORRECTED`, `RETRACTED`, `UNKNOWN`; 이전 사건 버전 참조 또는 null |
| affected_assets | 종목별 관련성·방향 분포·중요도·명제 판단; 복수 자산 허용 |
| model_id, prompt_version, raw_response_ref | 실제 요청·응답의 모델·질문 버전·원응답 참조; 고정 fixture는 명시 |
| expires_at | 사건의 사용 가능 기간 끝; 정보의 진실 여부 만료와는 구별 |

Noul은 statement와 값, Choice는 전체 선택지 분포, Score는 rubric_version과 등급 의미를 보존한다. `confidence`는 제공되는 경우 원 분포와 함께 저장하며 외표본 정답률·가격 예측의 보정 확률로 표시하지 않는다. 경제적 중요도와 예상 가격변화 크기는 구별한다.

복수 문서가 같은 사실을 인용한 관계를 보존한다. 문장 점수 평균·뉴스 개수·API의 질문별 독립 실행을 근거로 통계적 독립성을 가정하지 않는다. 가격점프의 확률·크기·강도는 향후 Layer 2에서 별도로 추정하며 이 계약의 Jev 판단값에 대입하지 않는다.

```json
{
  "schema_version": "0.1.0", "message_type": "EventPacket",
  "message_id": "event-demo-message-1", "run_id": "demo-20261103",
  "instrument_id": null, "source": "event-fixture", "source_event_id": "doc-demo-1",
  "event_at": null, "received_at": "2026-11-03T00:00:00.300000Z",
  "ready_at": "2026-11-03T00:00:00.900000Z",
  "data_origin": "SYNTHETIC", "quality_flags": ["SOURCE_TIME_UNKNOWN", "UNVALIDATED_MODEL"],
  "lineage": ["doc-demo-1"],
  "payload": {"event_id": "event-demo-1", "event_revision": 1, "event_type": "CONTRACT_ANNOUNCEMENT",
    "document_ids": ["doc-demo-1"], "evidence_refs": ["doc-demo-1:block-3"],
    "published_at": null, "published_date": "2026-11-03", "publication_precision": "DATE",
    "status": "ACTIVE", "supersedes": null,
    "affected_assets": [{"instrument_id": "SIM:EQ1",
      "relevance_noul": {"statement": "문서가 이 기업의 계약 체결을 명시하는가", "value": 0.8},
      "direction_probabilities": {"POSITIVE": 0.7, "NEGATIVE": 0.1, "MIXED": 0.05, "UNCLEAR": 0.15},
      "importance": {"rubric_version": "importance-demo-1", "score": 1.0, "legend": ["LOW", "MEDIUM", "HIGH"]}}],
    "model_id": "fixture-not-a-jev-call", "prompt_version": "event-demo-1", "raw_response_ref": "fixture-response-1",
    "expires_at": "2026-11-03T00:10:00.000000Z"}
}
```

## 7. UniverseSnapshot

생산자: 초기 seed, Phase 3 이후 Layer 0. 소비자: 구독·기업 연결·정책 후보 관리.

| payload 필드 | 형식·의미 |
| --- | --- |
| universe_id, revision, source_kind | 후보군 ID·버전·`SEED` 또는 `LAYER0` |
| as_of, valid_from, expires_at | 계산 기준 시각·적용 시작·적용 종료; 미래 정보는 포함하지 않음 |
| members | 상품 ID·InstrumentSpec 참조·기업 매핑 참조·선정 이유·배경 특징 참조 |
| excluded | 제외한 대상과 이유; 전체 거래소 전수 검사 여부는 coverage로 표시 |
| coverage, rule_version | 분석 대상 범위와 규칙 버전 |

기간은 `[valid_from, expires_at)`이며 소비자의 이용 가능 시각도 충족해야 한다. 후보에서 빠졌다는 이유로 기존 보유·주문 관리 구독을 해제하지 않는다. 매매 후보 구독과 위험 관리에 필요한 구독을 구별한다.

```json
{
  "schema_version": "0.1.0", "message_type": "UniverseSnapshot",
  "message_id": "universe-demo-1", "run_id": "demo-20261103",
  "instrument_id": null, "source": "manual-seed", "source_event_id": null,
  "event_at": null, "received_at": "2026-11-02T23:59:00.000000Z",
  "ready_at": "2026-11-02T23:59:00.010000Z",
  "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": ["spec-demo-eq1-v1", "entity-demo-1"],
  "payload": {"universe_id": "seed-demo", "revision": 1, "source_kind": "SEED",
    "as_of": "2026-11-02T23:59:00.000000Z", "valid_from": "2026-11-02T23:59:00.010000Z",
    "expires_at": "2026-11-03T06:30:00.000000Z",
    "members": [{"instrument_id": "SIM:EQ1", "instrument_spec_ref": "spec-demo-eq1-v1",
      "entity_map_ref": "entity-demo-1", "reason_codes": ["SYNTHETIC_TEST_ASSET"], "background_ref": null}],
    "excluded": [], "coverage": "ONE_SYNTHETIC_ASSET", "rule_version": "seed-demo-1"}
}
```

## 8. TradingPolicy

생산자: 초기 고정 정책 공급자, Phase 5 이후 Layer 2. 소비자: Layer 1의 신호·집행 경계. 정책은 주문이나 체결이 아니다.

| payload 필드 | 형식·의미 |
| --- | --- |
| policy_id, provider_kind, provider_epoch, revision | 정책 ID·`STATIC`/`LAYER2`·생산자 재시작 구분·상품/계좌별 증가 버전 |
| account_id, instrument_spec_ref, universe_ref | 적용 계좌·상품·후보군 버전 |
| valid_from, expires_at | 적용 구간. 미래 정책을 일찍 사용하지 않으며 만료 후 신규 진입 금지 |
| allowed_position_directions | 보유 허용 방향 `LONG`/`SHORT`; 보유분 매도와 신규 숏을 구별 |
| target_position_qty | signed 목표 **총 보유 수량**. 추가 주문 수량이 아님; 위험·진입 조건 충족 시에만 노출 증가 |
| entry | obi_long/obi_short, slope_min_per_second(null 가능), min_observations, min_duration_ms, max_observation_gap_ms |
| hold_exit | long_hold_obi/short_hold_obi, max_hold_ms, exit_target_qty, emergency_rule_ref |
| smoothing | 시간 기준 EMA의 tau_ms 또는 명시적 이벤트 기준 방법; 두 단위를 혼용하지 않음 |
| execution_limits | max_order_notional, max_position_qty, max_spread_bps, max_quote_age_ms, order_ttl_ms, max_retries, max_slippage_bps, price_reference_mode, price_reference_ref(null 가능) |
| risk_budget_ref | 계좌 전체 위험·미체결 예약 상태의 권위 있는 참조 |
| regime | null 또는 모델 버전·정의한 상태축·확률/불확실성; 초기 STATIC은 null |
| rationale, input_refs | 정책 목적·근거와 당시 이용 가능했던 입력 참조 |

진입·유지 OBI 값은 [-1,1]이고 롱의 진입 문턱은 유지 문턱보다 높다. 숏을 지원할 경우 반대 방향의 별도 문턱과 상품 제약을 확인한다. 기울기 필터, N회 지속성, 경과시간과 히스테리시스는 별도 조건이다. 진입 N회는 중복 없는 유효 관측이며 부적격 관측·허용 간격 초과 시 초기화한다.

max_hold_ms는 최초 체결 시각부터 계산한다. 추가 체결·재주문·정책 갱신으로 보유시간을 임의 초기화하지 않는다. 주문 의도 생성 시각과 실제 포지션 보유 시작을 구별한다.

청산 조건이 성립하면 exit_target_qty 또는 더 강한 위험 제한이 적용된다. 목표가 0이면 진입 조건 때문에 청산이 막히지 않는다. 집행기는 정책 목표와 실제 보유·미체결 예약을 함께 비교하며 이미 체결 중인 수량을 무시해 중복 발주하지 않는다.

정책은 전체 객체를 원자적으로 교체한다. (provider_epoch, revision)과 소비자 도착 기록으로 역순·중복을 처리하며 새로운 epoch 전환은 알려진 생산자 재시작 절차를 따른다. 다른 계좌·상품·실행의 정책은 사용하지 않는다.

price_reference_mode는 `AT_INTENT_CREATION` 또는 `FIXED_REFERENCE`이다. 전자는 주문 의도 생성 시 유효 호가 ID를 선택해 주문 의도에 저장한다. 후자는 price_reference_ref에 미리 지정한 유효 호가를 사용한다. 매수는 그 ask, 매도는 그 bid를 기준으로 max_slippage_bps와 명시한 가격 제한을 계산한다. 재시도할 때 기준 가격을 계속 옮겨 추격 한도를 무력화하지 않는다. 기준 호가를 조회할 수 없으면 새 노출을 늘리지 않는다. order_ttl_ms는 발송 이후의 유효시간이며 재시도 상한은 같은 주문 의도에 적용한다.

정책 만료·후보 제외·새 진입 금지 이후에도 살아 있는 주문·보유분을 관리한다. 취소·재주문·시장가 청산은 실제 주문 상태와 잔여 수량을 확인하는 절차를 거치며 시장가가 즉시 체결된다고 가정하지 않는다. 위험 한도·emergency_rule_ref는 정책 만료와 함께 사라지지 않는다.

```json
{
  "schema_version": "0.1.0", "message_type": "TradingPolicy",
  "message_id": "policy-demo-message-1", "run_id": "demo-20261103",
  "instrument_id": "SIM:EQ1", "source": "static-policy", "source_event_id": null,
  "event_at": null, "received_at": "2026-11-03T00:00:00.000000Z",
  "ready_at": "2026-11-03T00:00:00.010000Z",
  "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": ["universe-demo-1"],
  "payload": {"policy_id": "policy-demo-1", "provider_kind": "STATIC", "provider_epoch": "demo-epoch-1", "revision": 1,
    "account_id": "paper-demo", "instrument_spec_ref": "spec-demo-eq1-v1", "universe_ref": "universe-demo-1",
    "valid_from": "2026-11-03T00:00:00.010000Z", "expires_at": "2026-11-03T00:01:00.000000Z",
    "allowed_position_directions": ["LONG"], "target_position_qty": "1",
    "entry": {"obi_long": 0.6, "obi_short": null, "slope_min_per_second": null,
      "min_observations": 3, "min_duration_ms": 200, "max_observation_gap_ms": 500},
    "hold_exit": {"long_hold_obi": 0.2, "short_hold_obi": null, "max_hold_ms": 30000,
      "exit_target_qty": "0", "emergency_rule_ref": "paper-emergency-demo-v1"},
    "smoothing": {"method": "TIME_EMA", "tau_ms": 500},
    "execution_limits": {"max_order_notional": "20000", "max_position_qty": "1", "max_spread_bps": 20,
      "max_quote_age_ms": 1000, "order_ttl_ms": 1000, "max_retries": 0, "max_slippage_bps": 10,
      "price_reference_mode": "AT_INTENT_CREATION", "price_reference_ref": null},
    "risk_budget_ref": "risk-demo-v1", "regime": null,
    "rationale": "PLUMBING_TEST_ONLY", "input_refs": ["universe-demo-1"]}
}
```

예제의 임계치·수량·기간은 연결 검증용이다. 권장 매매 파라미터나 향후 Layer 2의 최적해를 의미하지 않는다.

## 9. 참조 레코드와 최소 재생 시나리오

JSON 예제의 외부 참조는 아래 합성 fixture를 #3 이후에 작성한다는 전제의 **명시적인 필요 입력**이다. 이 문서만으로 참조 대상이 이미 구현·저장되었다고 간주하지 않는다.

| 참조 | 필요한 내용 |
| --- | --- |
| spec-demo-eq1-v1 | SIM:EQ1, EQUITY, KRW, SHARE, lot_size=1, multiplier=1, can_open_long=true, can_open_short=false; 합성 호가/세션 규칙과 유효기간 |
| entity-demo-1 | 합성 기업과 SIM:EQ1 연결·가상 별칭·버전·이용 가능 시각 |
| doc-demo-1 / fixture-response-1 | 합성 원문 block-3와 수동으로 만든 출력값; Jev 실측 응답 아님 |
| risk-demo-v1 | paper-demo 계좌의 합성 현금·예약·총 노출 한도와 실행 모드 |
| paper-emergency-demo-v1 | 신규 진입 차단·주문 상태 확인·잔여 수량 청산 및 상태 불명 대조 절차 |
| account-demo-v2 | 초기 현금 1,000,000원에서 예시 체결·비용을 반영한 계좌 단일 버전 |
| raw-book-101…104 / intent-demo-1 | 중복 없는 호가 관측과 그 이후 생성된 주문 의도·시간·가격 제한 |

최소 재생 순서: seed와 정책 이용 가능 → 유효 호가 3회(00:00:01.000/01.100/01.200Z) → 경과시간·조건 충족 후 주문 의도 → 주문 이후 지연 조건을 충족한 01.300Z 호가 → 체결 통보 → 실제 수량·비용 원장 반영. 위 MarketSnapshot은 첫 호가 예시이므로 그 한 메시지만으로 진입하지 않는다.

추가 fixture는 중복 체결·취소 중 체결·오래된 호가·정책 만료·이벤트 정정·후보 제외·상태 불명을 포함한다. 구체 상태기계와 체결 모형의 구현은 #7–9에 속한다.

## 10. 계약 완료 판단

- 6개 유형의 메시지·필드·단위·생산자/소비자·이용 가능 시각·예시가 정의되어 있다.
- seed/고정 정책만으로 Layer 1·3 입력을 구성할 수 있고, 실제 API 접근 확인은 별도 선행 조건으로 드러난다.
- Layer 0·2의 교체는 같은 의미의 계약을 지키는 것으로 정의되며 미래 모형 산식은 미리 고정하지 않는다.
- 이 문서의 JSON 구문·참조·주요 수량/시간 불변식 검토와 실행 코드의 동작 검증을 구분한다.
