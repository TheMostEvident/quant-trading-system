# 고정 정책 신호·필터 (Issue #6)

`StaticSignalEngine`은 v0.2 `TradingPolicy`, `MarketSnapshot`, `AccountState`,
`PositionState`와 명시적 검증 근거를 받아 **목표 총 포지션**을 제안한다.
`provider_kind=STATIC`, `strategy_version=static-obi-v1`, `regime=null`만 실행한다.
Layer 2 예측·최적 베팅·수익성 검증을 의미하지 않는다.

## 실행과 산출물

```bash
quant-foundation signal-scenario tests/fixtures/static-policy.synthetic.json --output-dir data/signals
quant-foundation signal-scenario data/signals/inputs.json
python -m unittest discover -s tests -v
```

빈 출력 폴더에 `inputs.json`, `decisions.json`, `summary.json`을 쓴다.
저장 입력을 새 엔진에 순서대로 두 번 적용해 같은 결과인지 확인하고 입력·결과
SHA-256을 기록한다. 독립 수학 구현 검증이나 다른 플랫폼 간 bitwise 동일성을
주장하지 않는다. 기준 시나리오는 모두 SYNTHETIC이다.

이 CLI는 **오프라인 입력 bundle 평가**다. 호가 수집기에 자동 구독하거나 주문·잔고
API를 호출하지 않는다. 파일의 ready/seen 값은 명시된 입력의 조건이며 실제 전달의
증명이 아니다. 실제 소비자 연결 시 foundation의 t/W/D 경계에서 보인 메시지와
artifact만 bundle에 넣어야 한다. OMS·체결 원장·주문 예약·계좌 대조는 #7 이후이며,
Windows/OCX·실피드 적합성은 #33이다. 이 실행 결과를 실거래 검증으로 대체하지 않는다.

## 입력과 근거

Python API는 `prepare(bundle) → 결과 보존 → commit(update)` 또는 오프라인용
`process(bundle)`이다. 동시 writer는 지원하지 않으며 이전 상태를 기반으로 만든
transition을 뒤늦게 commit하면 거부한다. prepare만 한 관측은 N에 포함하지 않는다.
입력과 반환값은 직렬화해 동결하므로 호출자의 dict 변경이 내부 상태를 바꾸지 않는다.

| bundle | 의미 |
| --- | --- |
| decision_id, run_id, account_id, instrument_id | 계좌·상품별 평가 범위와 고유 결정 ID |
| as_of, clock | UTC 이용 가능 시점, producer_id/epoch/monotonic_ns가 있는 평가 시계 |
| seen_at | 각 메시지와 evidence가 이 소비자에게 보인 UTC 시각 |
| policy, market, account, position | v0.2 봉투; market=null은 타이머/위험 관리 평가 |
| evidence | `static-context-v1`: 동일 run/계좌/상품의 명세·capability·위험·시계 근거 |
| policy_transition_from | 정책 producer epoch 교체 시 직전 epoch를 명시 |

모든 입력에서 `ready_at ≤ seen_at ≤ as_of`를 확인한다. 계좌·상품·run·명세 참조가
일치해야 하고 PositionState는 전달한 AccountState의 message_id를 참조해야 한다.
누락·불명·대조 실패를 0 보유로 바꾸지 않는다. SYNTHETIC/OBSERVED를 섞지 않으며
INTERNAL_ONLY 대조 상태는 합성 입력에서만 허용한다. 관측 계좌는 MATCHED가 필요하다.
단순히 JSON에 MATCHED/verified를 썼다는 사실이 실환경 인증을 만들어 주지는 않는다.

evidence의 유효 구간은 명세·capability·후보군·위험 평가의 공통 적용 구간이다.
이를 만들 실제 provider가 해당 출처·가용성·유효성을 검증해야 한다. 이 Issue는
그 provider나 증권사 증거금 계산식을 구현하지 않는다. 예제의 증거금은 합성 fixture다.

| evidence 구성 | 검사 |
| --- | --- |
| evidence_id, ready_at, valid_from, expires_at, data_origin | 시점·출처·scope; `[valid_from, expires_at)` |
| instrument_spec_ref, universe_ref, risk_budget_ref, feature_config_ref | 정책과 동일한 참조; spec/feature config/위험 한도는 같은 참조에 다른 내용을 넣으면 거부 |
| specification | asset_class/unit/qty_step/min_qty/승수/손익 자산/tick/NET/LINEAR/capability/margin_rule_ref |
| feature_config | 실제 지표 깊이·가중치·평활화와 정책의 smoothing을 대조 |
| account_capability | verified와 can_open_long/short가 명시적으로 true일 때만 신규 방향 허용 |
| risk | 상품 수량·계좌 gross notional 한도, 다른 상품의 예약을 포함한 gross 노출, 시점 있는 증거금 quote |
| position_lifecycle | 해당 position_state_ref, 최초 체결 참조와 최초 체결의 monotonic clock |

계좌/포지션 revision은 감소할 수 없고 같은 revision에 다른 봉투를 넣을 수 없다.
정책도 epoch/revision을 검사하며 확인된 교체 없이 새 epoch를 쓰거나 퇴역 epoch로
돌아가면 거부한다. epoch 이력은 계좌·상품당 32회로 제한한다. 상태가 메모리에만
있으므로 재시작 시 저장 입력 prefix를 재생한다. 상태를 복원하지 않은 새 엔진은
지속성을 처음부터 세며, 보유시간은 외부 최초 체결 근거를 계속 요구한다.

## 진입 지속성과 경계

- 지원 지표는 `microstructure-v1`, RECEIVE_MONOTONIC, 현재 **CONTRACT**다.
  같은 명세·가격 단위·coverage·현재 raw lineage·깊이·가중치·smoothing을 확인한다.
- 정책의 `target_position_qty` 부호가 목표 방향을 정한다. 양수 목표가 OBI가 음수라는
  이유로 자동 숏으로 바뀌지 않는다. 목표 0은 대기 또는 기존 보유의 청산 목표다.
- 롱은 smooth OBI ≥ obi_long, 숏은 smooth OBI ≤ obi_short다.
  선택적 slope_min_per_second는 방향을 곱한 기울기가 해당 비음수 값 이상인지 검사한다.
  null이면 이 필터를 끈다. 필수 특징이 null이면 충족한 것으로 계산하지 않는다.
- 연속 유효 **BOOK** N회와 첫/마지막 해당 관측 사이의 최소 경과시간을 모두 충족해야 한다.
  관측 수는 N에서 포화한다. 같은 시각의 별도 BOOK는 N에 들어가지만 경과시간을 늘리지 않는다.
- gap ≥ max_observation_gap_ms, 임계치 미달, TRADE/STATUS, stale/GAP, 시장 중단,
  위험·capability 실패는 streak를 끊는다. 정책 revision·clock epoch·거래일·명세·config
  변경과 지표의 FIRST_BOOK/CONTEXT_RESET/GAP_RESET도 새 streak를 시작한다.
- 같은 BOOK를 다른 decision_id로 재평가하면 N과 경과시간을 늘리지 않는다. 만료·위험
  조건은 다시 검사한다. 같은 decision_id의 같은 입력은 같은 결정을 반환하고 다른 입력은 거부한다.
- 결정 처리 지연은 지속성의 경과시간에 포함하지 않는다. 정책의 도착·valid_from보다
  오래된 호가로 새 정책의 streak를 시작하지 않는다. 성공한 streak는 한 번 소비한다.

호가 나이는 평가 시계와 원천 feature clock의 **동일한 producer/epoch** 안에서 계산한다.
시계 매핑 없이 다른 프로세스의 monotonic 값을 빼지 않는다. 이 조건을 만족하지 못하면
신규 진입을 차단한다. UTC 수신 시각도 보조 stale guard로 확인한다. 계좌·포지션은
`state_max_age_ms`의 ready_at UTC guard를 사용한다. 실제 계좌의 원천 as-of 및 시계
매핑을 증명하는 수집·대조 연결은 #7/#33에서 추가해야 한다.

| 경계 | 처리 |
| --- | --- |
| OBI = 진입 문턱 | 진입 특징 조건 통과 |
| OBI = 유지 문턱 | 유지 |
| duration = 최소 기간 | 기간 조건 통과 |
| quote age = 최대 나이 | 사용 불가 |
| 관측 간격 = 최대 간격 | 지속성 초기화 |
| as_of = valid_from / expires_at | 활성 / 신규 증가 금지 |
| 보유시간 = max_hold_ms | EXIT 목표 0 |

## 유지·청산·반전

롱은 smooth OBI가 long_hold_obi **미만**, 숏은 short_hold_obi **초과**일 때 EXIT다.
진입 문턱과 유지 문턱 사이가 완충 구간이다. OBI 변화율의 부호는 기본 청산 조건이
아니다. EVENT_EMA의 α=1은 현재 OBI를 그대로 반환하도록 지표 계산의 cancellation
오차도 수정했다. 과거 코드의 α=1 캐시는 마지막 자리 차이로 엄격 재계산 검사에
실패할 수 있으므로 원래 code/wheel을 보존하고 새 실행으로 다시 계산한다. 저장 이력은 수정하지 않는다.

max_hold_ms는 최초 체결 clock부터 계산하고, 부분 축소·재주문·정책 갱신으로
기산점을 바꾸지 않는다. flat 확인 없이 최초 체결 근거나 보유 부호가 바뀌면 REVIEW다.
정책 만료는 신규 진입·증가만 막으며 기존 유지·문턱 청산·보유시간 한도·목표 축소는
계속 평가한다. 만료 정책을 이전 미만료 revision으로 되돌리지 않는다. 호출자는
마지막 알려진 정책을 만료 후에도 전달해야 하며 정책이 없으면 REVIEW다.

반대 signed 목표를 받으면 먼저 EXIT/0을 내고 기존 PositionState version을 기억한다.
예약·active_orders가 없고 더 최신 version에서 flat이 확인된 뒤 새 방향의 N·기간을
처음부터 평가한다. 신호만 보고 flat이나 체결을 만들지 않는다. 미체결·청산 경합이
있으면 CANCEL_REVIEW로 후속 OMS의 취소·대조를 요구하며 직접 취소 API를 호출하지 않는다.

타이머의 최대 보유시간, 명시적 flat 목표와 정책 축소는 호가 없이도 **감소 목표**를
낼 수 있다. quote_ref=null인 목표는 실행 가격이나 체결 보장이 아니다. 후속 OMS가
최신 호가·주문 가능 세션·잔량·가격 상한과 비상 규칙을 확인해야 한다. 이 Issue는
시장가 주문이나 '청산 실패 시 즉시 전량 시장가'를 실행하지 않는다.

## 위험 한도와 상품 경계

신규 증가에서 abs(target), 보유±매수/매도 예약의 최대 절대 수량을 함께 확인한다.
예약 또는 active order가 하나라도 있으면 우선 대조하며 새 목표를 집행 가능한 것으로
표시하지 않는다. 현재 구현은 선형 선물·NET만 지원한다. 비선형·hedge mode를 추정하지 않는다.

명목가치는 exact Fraction/Decimal로 계산한다. 보수적 가격은
`max(abs(best_bid), abs(best_ask)) + max_slippage_ticks × tick_size`다.
주문 명목가치는 `abs(target−actual) × 가격 × 승수`, 계좌 gross는 이 상품의 최악 수량과
다른 상품의 예약 포함 gross 합이다. 음수 선물 가격도 abs notional을 사용한다.
자산이 다른 금액을 암묵 환산하지 않는다. 주문/수량/계좌 gross 한도를 각각 검사한다.

선물 증거금 quote는 **현재 account_state_ref·position_state_ref·quote_ref·target·rule**에
묶인다. total_initial_required와 incremental_required를 별도로 받아 초기 사용액과
주문 예약액을 포함한 한도, 가용 증거금을 검사한다. available_margin은
NET_OF_ORDER_RESERVED로 정의하고 예약액을 다시 빼지 않는다. 증분 필요액은 최소한
`max(total_required−initial_used, 0)` 이상이어야 한다. 실제 브로커의 모든 위험 시나리오를
이 단순 검사가 대체하지 않으며, 검증된 quote provider가 없으면 신규 증가하지 않는다.

기존 수량/gross 한도 초과나 불명·음수 가용 증거금은 정상 HOLD로 표시하지 않고
REVIEW한다. 명시적인 감소·청산 목표는 계속 제안할 수 있다. 세션 중단·state 불명·
시계 불일치에는 신규 위험을 추가하지 않으며 기존 보유를 임의로 0으로 만들지 않는다.

크립토 현물은 BASE_ASSET, 주식 현물은 SHARE와 CASH 원장을 요구한다. 현재는 보유량
이하의 명시적 목표 감소/0 청산과 예약 초과 처분 차단을 지원한다. **현물 신규 진입·
OBI 유지 전략은 아직 지원하지 않는다.** CONTRACT-only 지표를 현물로 재명명하지 않으며
선물 숏·증거금을 적용하지 않는다. 현물 어댑터·단위별 지표 지원은 후속 범위다.

## 출력과 검증

| action | 의미 |
| --- | --- |
| WAIT | 신규 증가 조건 미충족; target=null은 주문 목표 없음 |
| ENTER / INCREASE | 필터·확인한 제약을 통과한 signed 총 목표 제안 |
| HOLD | 전달받은 실제 보유량을 유지하는 목표 |
| EXIT / REDUCE | 0 또는 더 작은 총 목표; 실제 체결은 별도 |
| REVIEW / CANCEL_REVIEW | 근거·상태/시계·위험·미체결 대조 필요; 실행 목표 없음 |

각 결과에 policy/input/evidence/quote 참조, 실제 수량, 단위, reason_codes, N·기간,
위험 계산을 기록한다. orders_created=0이며 PositionState를 갱신하지 않는다.
이 결과는 기존 7개 메시지 타입에 추가한 새 주문 계약이 아니다.

[고정 worksheet](../tests/fixtures/static-policy-expected.json)의 예상 결과는 구현 실행값으로
채우지 않았고, [합성 입력](../tests/fixtures/static-policy.synthetic.json)은 실제 FeatureEngine으로
만들어 동결했다. 350ms와 650ms의 보유 변화는 외부 원장 fixture를 명시적으로 공급한다.

| ms | 입력 보유 | 조건 | 예상 action / 총 목표 |
| --- | --- | --- | --- |
| 0 / 100 / 200 | 0 | OBI .8, N=3, 기간 200ms | WAIT / WAIT / ENTER 2 |
| 300 | 0 | 앞의 신호 이후에도 원장은 flat | WAIT |
| 400 | 2 | OBI .2 = 유지 문턱 | HOLD 2 |
| 500 | 2 | OBI .19 < 유지 문턱 | EXIT 0 |
| 600 | 2 | 새 정책 목표 −2, 아직 원장은 롱 | EXIT 0 |
| 700 / 800 / 900 | 0, 새 version | OBI −.8, 새 streak | WAIT / WAIT / ENTER −2 |

현재 **198개 테스트(기존 125 + 정책 73)**가 지표 생산→정책 평가, 경계·정책 만료,
동시 transition·중복·최초 체결 기산점·반전 확인·위험·현물 제한·CLI 보존/재생을 검증한다.
설치 환경·wheel·개별 시험·소스 hash는 [검증 JSON](verification/policy-20261104.json)에 기록한다.
