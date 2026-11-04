# Issue #8: 지연·깊이·비용을 반영한 내부 모의 집행

`synthetic-depth-highwater-v1`은 **합성 선형 선물 모의 집행기**다. 이미 병합된 #7의 transport claim을 받아 이후의 명시적 관측으로 ExecutionReport를 생성하고 기존 원장에 반영한다. 실제 브로커 호출은 없다. #33 완료와 독립적으로 개발하고, 실제 선물 자료에 대한 가정/지연/비용 대조는 **Extra Issue 2 (#43)**로 추적한다.

## 실행

저장소를 설치한 core 환경에서 실행한다. 출력 폴더는 비어 있어야 한다.

```powershell
.\.venv-core\Scripts\python.exe -m quant_system paper-scenario tests\fixtures\paper.synthetic.json --output-dir data\paper-demo-001
.\.venv-core\Scripts\python.exe -m quant_system integrity data\paper-demo-001\history.db
.\.venv-core\Scripts\python.exe -m quant_system replay data\paper-demo-001\manifest.json --run-id paper-replay
.\.venv-core\Scripts\python.exe -m quant_system paper-scenario data\paper-demo-001\inputs.json --output-dir data\paper-demo-002
```

같은 입력에서 trace/state/paper-state hash를 재현한다. 새 DB의 store ID가 포함되는 manifest 자체의 동일성은 주장하지 않는다. manifest 전달 재생의 배속 1/1000 일치와 OMS 입력부터 원장 상태의 재계산도 확인한다. 임의 운영 DB의 변조 방어나 실제 수익성 보장은 아니다.

## 관측과 체결 순서

- `TARGET`의 고정 총 목표 수량·현재 포지션·정책·호가·계좌 한도는 #7이 검사한다. 허용액을 목표 수량으로 자동 확대하지 않는다. `DISPATCH`가 영속 claim을 반환한 command만 모의 집행기에 전달한다.
- 설정한 양의 주문 지연 뒤에 주문이 도착한다. 별도 통보 지연이 지난 ACK/FILL/CANCEL_ACK만 OMS가 알 수 있다. 체결 시각과 통보 이용 가능 시각을 구별한다.
- 체결은 **주문 claim보다 엄격히 늦고 주문 도착 이후 수신된 새 관측**에서만 발생한다. 캐시된 이전 호가가 늦게 전달돼도 체결하지 않는다. 동일 시점 중간가격, 오래된 decision quote, timer만으로 체결하지 않는다.
- 매수는 ask, 매도는 bid를 가격순으로 소진한다. 부분체결은 각 level 가격별 별도 fill이다. 추가 불리한 tick 이동을 적용한 가격도 원래 지정가 안에 있어야 한다.
- UNKNOWN/HALTED/UNSYNCED, 품질 플래그, stale, 다른 run/origin/spec 또는 잘못된 tick/정수 수량/깊이는 체결에 사용할 수 없다. clock domain 변경·시간 역행·UTC/monotonic 불일치는 실행을 중단한다.
- 취소는 별도 지연 뒤 도착한다. 그 이전 관측에서는 체결할 수 있다. 동일 시각이면 도착/취소/만기 처리를 먼저 하고 호가를 본다. 취소 전에 전량 체결되면 CANCEL_REJECT다. 부분 취소/만기 후의 예약 해제·대조 보수성은 #7 그대로다.
- 계약별 명시적 합성 만기에 남은 주문은 EXPIRE이고 만기 후 도착한 신규 주문은 REJECT다. 다른 월물의 호가로 기존 월물 주문을 체결하지 않는다. 실제 만기 달력이나 자동 롤오버 규칙을 추정하지 않는다.

## 유동성의 보수적 하한 모형

top-five snapshot만으로 실제 지정가 대기열, 취소/재추가 물량, 반복 관측이 나타내는 새로운 유동성을 확정할 수 없다. 따라서 각 **상품·side·가격의 세션 내 최대 관측 수량**을 상한으로 삼고 이미 소비한 수량을 차감한다. 매 관측에서도 현재 보이는 수량과 참여율·정수 step·전체 book별 체결 한도를 넘지 않는다.

예: ask 101에 1계약을 써서 체결했으면, 같은 snapshot 재전달이나 다음 snapshot의 동일 1계약을 다시 쓰지 않는다. 이후 2계약이 보이면 추가 1계약까지 인정한다. 여러 주문이 같은 관측을 공유한다. 수량 감소 후 회복이나 새 주문 도착만으로 소비 이력을 초기화하지 않는다.

이 규칙은 실제 유동성 재공급을 과소평가할 수 있는 **명시적 합성 하한 가정**이다. 유일하게 옳은 시장 모형이라는 주장이 아니다. 수동 지정가 대기열 체결은 만들지 않으며 이후 반대편 호가와 교차한 때만 후보가 된다. 실제 자료로 이 가정의 적합성을 확인하는 작업은 #43이다.

## 고정 설정과 비용

`assumptions`에는 model/origin, 주문·취소·통보 지연(ms), 최대 호가 나이, 참여율, 불리한 slippage tick, book별 최대 계약 수, 계약별 만기 UTC, 계약별 비용과 근거 설명을 기록한다. 생략한 비용/증거금/만기를 실제 조건으로 자동 채우지 않는다.

비용은 `quantity × per_contract + abs(price) × quantity × multiplier × notional_rate`의 FEE와 별도 tax_rate의 TAX다. 설정한 담보 자산으로 분개하며 임의 통화 환산을 하지 않는다. 선물의 명목가치를 현물 매수대금처럼 차감하지 않는다. 가격·수량·비용은 exact Fraction 연산이며 실제 통화 절사·세금·브로커 반올림은 별도 검증이다. 예시의 승수 1000·계약당 비용 20·고정 증거금·지연 값은 시험값이다.

첫 실행 모드는 SYNTHETIC_REPLAY, 상품 범위는 명시적 CONTRACT/NET 선형 선물이다. OBSERVED 원천을 라벨만 바꿔 투입하지 않는다. 실제 원천과 합성 체결을 함께 보존하는 recorded/shadow 연결, 현물의 소수 수량·수수료 자산별 집행 모형, 실제 계좌 대조 provider는 후속 범위다. 기존 현물 원장 지원이 현물 집행 모형 완료를 뜻하지 않는다.

## 저장과 재생

각 paper input을 먼저 raw capture로 저장한다. 시장 관측과 가정은 artifact로 보존하고, 생성 report는 #7의 raw-first/원자적 receipt·projection 경로로 적용한다. command claim을 남기기 전에 모의 주문을 전달하지 않는다. 외부 REPORT/RECONCILE 입력으로 모의 체결을 주입하는 것은 이 runner에서 거부한다.

출력은 `history.db`, `inputs.json`, `trace.json`, `paper-state.json`, `manifest.json`, `summary.json`이다. trace에는 공개된 report 이용 시점과 원장 상태, 시장 관측 처리 이유가 있다. summary의 pending report/command와 미체결 수량은 구분한다. 입력 종료 시 pending 통보나 미체결을 자동 완료로 바꾸지 않는다. 장래 만기 timer는 command 전송 대기 수에 포함하지 않는다.

중간 오류에서는 이미 보존한 source/DB를 남기고 성공 summary를 만들지 않는다. 실행 중 매칭 상태의 crash-resume은 이 버전에서 지원하지 않는다. 비어 있는 새 출력에서 보존 입력 전체를 재실행한다. 취소/만기의 미확정 예약과 재시작 상태를 실제 계좌 대조 없이 풀지 않는다.

## 독립 예상표

`tests/fixtures/paper.synthetic.json`은 기존 OMS의 명시적 fill을 제거하고, 15개 주문/시장/평가/정산 입력에서 **집행기가 fill 4개를 생성**하게 한다. [수작업 예상값](../tests/fixtures/paper-expected.json)은 구현 결과를 복사해 만들지 않았다.

| 단계 | 보유 계약 | KRW 현금 | KRW 평가자산 |
| --- | --- | --- | --- |
| 초기 | 0 | 1,000,000 | 1,000,000 |
| 101에 1계약씩 두 번, 비용 각 20 | 2 | 999,960 | 999,960 |
| 102 평가/정산 | 2 | 1,001,960 | 1,001,960 |
| 103에 2계약 청산, 비용 40 | 0 | 1,003,920 | 1,003,920 |
| 101에 숏 2계약, 비용 40 | −2 | 1,003,880 | 1,003,880 |
| 100 평가 | −2 | 1,003,880 | 1,005,880 |

새 시험은 지연/보고 시각·캐시/낙관적 체결 방지·깊이/중복 유동성·취소 경합·한도·만기/월물·비용/원장·CLI/재생을 확인한다. 합성 검증 결과로 #33이나 #43을 완료 처리하지 않는다. 전체 추진 규칙은 [AGENTS.md](../AGENTS.md)와 로드맵 #1을 따른다.

Linux Python 3.12.14 / SQLite 3.53.1에서 새 31개를 포함한 전체 343개 시험이 소스와 별도 설치 wheel 각각 통과했다. 설치본의 41개 Python 모듈은 검증한 소스와 바이트가 일치하고 pip check도 통과했다. [검증 기록](verification/paper-20261104.json)에 source/wheel/trace hash와 미검증 범위를 남겼다. 최신 revision의 Windows 시험은 별도다.
