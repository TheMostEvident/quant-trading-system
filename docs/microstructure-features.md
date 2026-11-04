# 미시구조 특징 microstructure-v1

2026-11-04 (Asia/Seoul) · [Issue #5](https://github.com/TheMostEvident/quant-trading-system/issues/5) · 선행 구현 PR #34

가중 OBI·관측 OFI·EMA·차분·스프레드/깊이·거래량을 계산하고 MarketSnapshot.features에 보존한다. 계산 정의는 설정 artifact에 고정한다. **125개 unittest**(기존 83 + 지표 42)로 손계산 기준·경계·시점·상태 초기화·독립 재계산을 검증했다. Windows/OCX·실제 피드의 단위/범위/수신 검증은 [#33](https://github.com/TheMostEvident/quant-trading-system/issues/33)에 남아 있다.

Issue #6에서 EVENT_EMA α=1일 때 현재 OBI를 정확히 반환하도록 cancellation 오차를
수정했다. 기존 α=1 캐시의 마지막 자리와 달라질 수 있으므로 과거 엄격 재계산에는
원래 code/wheel을 보존한다. [고정 정책 안내](static-policy.md)의 P16 경계 회귀와
현재 198개 시험이 이 변경을 포함한다. 기본 TIME_EMA 경로는 변경하지 않았다.

## 구현과 계약

| 경로 | 책임 |
| --- | --- |
| `market/features.py` | 순수 계산·producer clock·bounded windows·prepare/commit 상태 전이 |
| `contracts/microstructure.py` | microstructure-v1의 단위·nullable·clock·TRADE/BOOK 의미 검증 |
| `market/feed.py` | 원천/계보/설정 저장·정규화 결과에 특징 연결·메시지 커밋 후 상태 반영 |
| `market/feature_replay.py` | manifest의 정규화 입력·raw 수신 clock으로 다시 계산해 캐시 대조 |
| `config/features.example.json` | 깊이·가중치·평활화·시간창·간격/메모리 상한 |
| `tests/fixtures/microstructure-hand-checked.json` | 고정 산술 worksheet·독립 Decimal.exp 기준값 |
| `tests/fixtures/kiwoom-features.synthetic.jsonl` | 실제와 구별되는 합성 callback·재접속·거래·stale 예시 |

메시지 schema_version은 0.2.0을 유지하며 feature_version으로 새 의미를 구별한다. 이전 합성 계약 예시는 유지한다. 현재 stateful calculator는 **CONTRACT** 수량을 지원한다. price_unit은 상품 명세에서 명시해야 하며 통화/가격 단위를 추정하지 않는다. Features는 신호·주문·수익 확률이 아니다. 고정 정책은 #6, 주문/계좌는 #7 이후의 작업이다.

## OBI와 관측 OFI

깊이 N과 음이 아닌 가중치 w를 고정한다. B/A는 각 측의 활성 수량이다.

$$
B^w_n=\sum_{i=1}^{N} w_i B_{n,i},\quad A^w_n=\sum_{i=1}^{N} w_i A_{n,i},\qquad
\mathrm{OBI}_n=\frac{B^w_n-A^w_n}{B^w_n+A^w_n}.
$$

분모 0은 null이다. 기본은 N=5·모든 가중치 1이며 최적화한 값이 아니다. 알려진 top-5 coverage 안의 수량 0인 빈 rank만 0으로 취급하고 관측 범위 밖을 0으로 채우지 않는다. 최우선 queue imbalance 문헌을 참고한 **프로젝트의 rank 가중 확장**이다. 근거: [Gould–Bonart, queue imbalance](https://arxiv.org/abs/1512.03492).

최우선 bid/ask 가격 p와 수량 q의 연속 관측을 비교한다.

$$
e_n=\mathbf{1}_{\{p^b_n\geq p^b_{n-1}\}}q^b_n
-\mathbf{1}_{\{p^b_n\leq p^b_{n-1}\}}q^b_{n-1}
-\mathbf{1}_{\{p^a_n\leq p^a_{n-1}\}}q^a_n
+\mathbf{1}_{\{p^a_n\geq p^a_{n-1}\}}q^a_{n-1}.
$$

가격이 같으면 bid 수량 증가와 ask 수량 감소가 양수다. bid 상승은 새 bid 수량, bid 하락은 이전 bid 수량의 음수, ask 하락은 새 ask 수량의 음수, ask 상승은 이전 ask 수량이다. 근거: [Cont–Kukanov–Stoikov, §2.1](https://arxiv.org/abs/1011.6402).

현재 피드에서는 이를 **snapshot 사이의 관측 OFI**로 표시한다. 중간 이벤트가 보이지 않으면 완전 이벤트 OFI와 다를 수 있다. 예를 들어 bid (100,10)→(101,7)→(100,9), ask 불변일 때 두 step 합은 0이지만 중간 snapshot을 빠뜨린 endpoint 비교는 -1이다. `OBSERVED_SNAPSHOT_OFI` 플래그를 남긴다. 취소와 시장가 체결을 이 값만으로 분해하지 않는다. 문헌의 주식 예측 성능을 국내 선물 수익률로 이전하지 않는다.

## 평활화·시각·단위

수신 monotonic 간격 Δt를 사용한다. 시간 EMA는 다음과 같고 첫 관측은 S=OBI로 시작한다.

$$
\alpha_n=1-e^{-\Delta t_n/\tau},\qquad
S_n=S_{n-1}+\alpha_n(\mathrm{OBI}_n-S_{n-1}).
$$

작은 간격에서도 가중치를 잃지 않도록 `-expm1(-dt/tau)`를 사용한다. EVENT_EMA를 선택하면 alpha를 고정한다. 두 방식의 설정을 섞지 않는다. 0시간 관측은 시간 EMA를 바꾸지 않지만 event EMA와 관측 OFI는 새 관측으로 처리한다. 이때 초당 변화율은 null이다.

| 필드 | 정의·단위·첫 값 |
| --- | --- |
| `obi`, `smooth_obi` | 무차원 [-1,1]; 첫 S=OBI, 분모 0이면 null |
| `obi_slope_per_second` | (현재 S−이전 S)/Δt초; 첫 관측·Δt=0은 null |
| `details.raw_obi_slope_per_second` | raw OBI 차분/Δt초; 같은 nullable 규칙 |
| `details.*_delta_per_observation` | 관측당 raw/smooth 차이; 초당 변화율과 구별 |
| `ofi_top1_delta`, `ofi_window_sum` | signed CONTRACT 정수; 첫 OFI/정보 부족/overflow 합은 null |
| `details.bid_depth/ask_depth` | 요청한 첫 N rank의 gross CONTRACT, decimal string |
| `details.weighted_*_depth` | 가중 CONTRACT; 소수 가중치 보존, decimal string |
| `details.spread/mid_price` | 가격 단위의 ask−bid / 평균; exact decimal string, 음수 중간값도 보존 |
| `details.spread_ticks` | 명시된 고정 tick_size로 나눈 정수; 미확인/나누어떨어지지 않으면 null·플래그 |
| `details.trade_volume` | BUY/SELL/UNKNOWN별 gross CONTRACT, decimal string |
| `details.signed_trade_volume` | UNKNOWN 수량이 0이고 window loss가 없을 때 BUY−SELL; 그 외 null |

가격·수량 문자열을 float 가격으로 바꿔 비교하지 않는다. 가격/가중 합은 Fraction과 국소 decimal context로 보존한다. EMA·비율·변화율은 finite float다. 계산 표현의 상한은 한 레벨/체결 10^12 계약, window 최대 4096개, 가격/가중 문자열 64자다. 이는 구현 표현/메모리 한도이며 브로커 거래 한도·베팅 한도가 아니다.

TRADE에는 새 OBI·OFI delta·book 깊이/스프레드를 복사하지 않는다. 해당 거래량 window만 갱신한다. BOOK에서도 그때까지 관측한 거래량을 볼 수 있다. UNKNOWN 방향을 가격/수량 부호만 보고 BUY/SELL로 채우지 않는다. 고정 tick 정보가 없으면 가격 단위 spread만 계산한다. TICK_SIZE_MISMATCH는 feature 진단이며 전체 상품별 tick/주문 규칙 검증기를 대신하지 않는다.

## 시간창·초기화·커밋

- OFI와 거래량은 producer monotonic 기준 **(t−W, t]**를 사용한다. 왼쪽 경계와 같은 시각의 관측은 제외한다. 동일 시각의 별개 관측은 각각 포함한다.
- 첫 OFI는 이전 book이 없으므로 null이다. 처음부터 full window를 관측했다고 표시하지 않는다. `*_window_complete`는 **저장된 관측의 시간 범위/메모리 coverage**만 뜻하며 거래소의 모든 상류 이벤트를 수신했다는 뜻이 아니다.
- window에는 최대 max_window_events개를 보존한다. 아직 시간창 안에 있는 원소가 메모리 한도로 밀려나면 OFI sum/거래량을 null, complete=false로 표시한다. 누락이 window 밖으로 나가야 복구된다. count는 보존한 관측 수다.
- GAP·STALE·역순·clock 오류·부적격 상태·UNSYNCED book은 기존 계산을 폐기한다. producer epoch·spec 참조·거래일·단위 변경도 새 구간으로 시작한다. 첫 새 OBI를 기준으로 S를 다시 시작하고 OFI/rate는 null이다.
- 가중 분모가 0이면 이전 평활값·flow 구간을 이어 쓰지 않는다. ZERO_WEIGHTED_DEPTH를 표시하고 다음 계산 가능한 book에서 다시 시작한다.
- 긴 간격은 새 구간이다. 실제 상한은 feature max_observation_gap_ms와 source stale_after_ms 중 작은 값이고 **같아지는 경계부터** 초기화한다. heartbeat/trade가 있더라도 오래된 book을 이어 쓰지 않는다.
- `book_observations`는 수학적 계산 구간의 관측 수다. 거래 가능한 N회 진입 필터가 아니며 quote gate/정책 조건을 별도로 적용해야 한다.
- 특징을 prepare하고 메시지를 DB에 커밋한 뒤에만 내부 EMA/window 상태를 commit한다. 계산 중 stale이 되면 특징을 버린다. live ready_at은 지표 계산 뒤 샘플링하고 t/W/D·consumer seen 규칙을 유지한다.

계산 함수에는 wall clock·sleep·네트워크·미래 데이터 접근이 없다. 동일 입력 ID의 즉시 재전달은 수학 상태를 바꾸지 않는다. source spool의 과거 재전달은 앞단의 frame ID 처리로 제거하고 재생에서는 producer의 고유 메시지를 한 번씩 계산한다. 역순/ID 충돌은 정상 관측으로 세지 않는다.

## 손계산 기준

N=2, w=(1,0.5), tau=1000ms, W=1000ms인 고정 worksheet다. 각 행은 독립적으로 계산 가능한 가격·수량과 기대값을 JSON에 보존했다. EMA 기대값은 구현의 math.expm1과 별도로 60자리 Decimal.exp로 평가했다.

| 관측 | t(초) | weighted bid/ask | OBI | 관측 OFI delta | window sum |
| --- | ---: | --- | --- | ---: | ---: |
| a | 0 | 12 / 10 | 1/11 | null | null |
| b | 1 | 15 / 8 | 7/23 | 5 | 5 |
| c | 1.25 | 9 / 7 | 1/8 | 11 | 16 |
| d | 2 | 11 / 9 | 1/10 | -12 | -1 |

d의 window는 (1,2]이므로 b의 5는 빠지고 c의 11과 d의 -12만 남는다. bid/ask 상승·하락·동일 가격과 수량 변화, 관측 사이에 trade가 끼어도 book EMA가 바뀌지 않는 경우도 별도 시험한다.

## 실행과 캐시 독립 재계산

```bash
python -m unittest discover -s tests -v
python -m quant_system market-ingest --input tests/fixtures/kiwoom-features.synthetic.jsonl --database data/features.db --run-id features-demo --config config/kiwoom.synthetic.json --feature-config config/features.example.json --simulate-clock --manifest data/features.manifest.json
python -m quant_system verify-features data/features.manifest.json --run-id features-replay --speed 1
python -m quant_system verify-features data/features.manifest.json --run-id features-replay --speed 1000
python tools/benchmark_market.py --events 200
```

합성 fixture는 raw 10건→메시지 11건, feature 4건(BOOK 3 + TRADE 1)이다. fixture에서 수량이 바뀌는 step의 관측 OFI=3, 가중 OBI=13/51을 얻고 재접속 뒤 OFI는 다시 null이 된다. 마지막은 stale로 이용 불가다. 이 숫자는 실제 상품/실측 시세가 아니다.

`verify-features`는 저장된 정규화 book/trade와 **raw callback의 수신 시계**를 입력으로 사용한다. config/spec artifact와 producer journal 순서를 복원하고 각 근거의 commit·ready cutoff를 검사한다. consumer에게 직접 전달되지 않았어도 계산 근거로 들어간 producer 선행 메시지를 복원한다. ACK 유실 재전달로 지표를 두 번 갱신하지 않는다. 정규화된 가격을 원시 FID에서 다시 파싱하는 도구는 아니며 그 경로는 수집기 시험에서 검증한다.

캐시·hash를 그대로 읽어 성공했다고 하지 않는다. 새 결과와 저장된 features의 모든 필드를 대조하고, 잘못된 OBI/clock을 넣고 root hash까지 다시 계산한 캐시도 실패시킨다. 미래 basis 역시 거부한다. feature가 없는 manifest를 검증 성공으로 표시하지 않는다. 같은 manifest·target run·검증한 runtime에서 배속에 무관한 결과를 요구한다. 임의 OS/수학 runtime 간 bitwise 일치는 별도 검증 대상이다.

## 검증과 잔여 범위

| 시험 | 확인 |
| --- | --- |
| F01–F06 | 고정 산술 표·OFI 가격 분기·가중 깊이·분모 0·시간/event EMA |
| F07–F12 | 동일/나노초 시각·불규칙 간격·half-open window·거래 방향/거래량 분리 |
| F13–F19 | 품질/상태/epoch/spec/date/unit 초기화·gap 경계·역순·재전달·prepare/commit |
| F20–F29 | bounded overflow·decimal context·음수 가격·tick 미확인·단위/설정/schema·시점 인과성·관측 OFI 한계 |
| F30–F40 | end-to-end·raw clock 재계산·producer 선행 근거·ACK 유실·허위 캐시/미래 basis·publish 실패·CLI·재전달·빈 입력 거부 |
| F41–F42 | 계산 중 stale 전환·watchdog 없이 source freshness 경계 도달 |

Linux 64-bit / Python 3.12.14 / SQLite 3.53.1에서 원래 83개와 위 42개, 총 **125개**가 통과했다. 별도 venv의 wheel 설치·CLI·재생·기계 판독 결과와 소스 hash는 [검증 JSON](verification/features-20261104.json)에 남긴다. [기존 수집 시험](verification/kiwoom-20261104.json)은 PR #34 시점의 이력이다.

최종 설치 환경의 합성 full-book 200건 순차 측정은 0.773초(약 258.9건/초)였다. raw+정규화/특징 commit p95/p99는 3.470/4.160ms, callback 입력→consumer ACK는 5.524/6.777ms였다. raw 202건·메시지 203건에서 BOOK 특징 200건을 독립 재계산해 모두 대조했다. WAL 32,502,712바이트, PASSIVE checkpoint busy=0 및 7,889/7,889 frame을 확인했다. 이는 임시 환경의 합성 단일 프로세스 측정이며 실제 시장 수신 지연·burst/HFT 처리 능력이나 실거래 성능의 합격선이 아니다.

실제 선물의 quantity_unit·price_unit·tick/coverage·수신 clock 의미·지표 입력 적합성은 #33에서 검증한다. 최종 가중치·평활화 시간·지표의 예측력·베팅/수익성은 선정하지 않았고 이 PR의 시험으로 검증한 것도 아니다. Layer 2 연구·구현 순서와 별개로 고정 지표 계산기를 제공한다.
