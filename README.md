# quant-trading-system

Research and development of a layered quantitative trading system.

현재 단계는 **연구 설계**이다. 실행 코드·수집기·주문 엔진은 아직 구현하지 않았다.

## 거래 범위와 첫 연결

| 항목 | 사용자 선택 |
| --- | --- |
| 목표 상품 | 국내 주식 현물, 국내 선물, 해외 선물, 크립토 현물 |
| 첫 수집·모의매매 연결 | **국내 선물** |
| 우선 검토 서비스 | 키움증권, 바이낸스 |
| 국내 선물의 API 검토 경로 | 키움 OpenAPI+의 공식 지원 상품부터 검증 |
| 크립토의 현재 범위 | 현물; 거래쌍·계좌·실제 접근은 후속 검증 |

국내 선물의 첫 계약과 나머지 시장의 연결 순서는 아직 정하지 않았다. 사용자 선호와 실제 계좌/API 접근 확인은 구별한다.

## 구축 순서

| Phase | 대상 | 범위 |
| --- | --- | --- |
| 0 | 공통 준비 | 범위·계약, 원천 보존·재생 기반 |
| 1 | Layer 1 | 국내 선물부터 호가·체결, 미시구조 특징, 고정 정책, 모의 집행 |
| 2 | Layer 3 | 공시·뉴스, 근거, 이벤트·자산 연결, Jev 판단 |
| 3 | Layer 0 | Info Source 확정, 상품·배경 자료, 시점별 후보군 |
| 4 | Layer 2 연구 | 금융수학·확률제어, 모형·정책·사이징 연구 |
| 5 | Layer 2 구현 | 상태 추정, 동적 정책, 전체 통합 검증 |

Layer 1·3은 seed 목록과 고정 정책으로 먼저 검증한다. Layer 2 구현은 Layer 0와 선행 연구 뒤에 진행한다. 거래 대상 확대가 네 Layer의 구축 순서를 바꾸지는 않는다.

## 설계 문서

- [현재 범위·구조](docs/scope.md)
- [현재 메시지 계약 v0.2](docs/contracts-v0.2.md)
- [상품별 API·실행 환경 비교](docs/source-evaluation.md)
- [공통 데이터 기반 설계](docs/foundation-design.md)
- [저장·시점·재생 검증 계획](docs/foundation-validation-plan.md)
- [결정 기록](docs/decisions.md)
- [이전 현물 중심 계약 v0.1 — 이력 보존](docs/contracts-v0.1.md)

선물의 계약승수·증거금·정산·만기와 현물의 자산별 잔고를 구분한다. 모든 예시 수치·정책은 합성이며 실제 상품 사양·매매 추천·성능 추정치가 아니다.

## 작업 관리

- [전체 로드맵 #1](https://github.com/TheMostEvident/quant-trading-system/issues/1)
- [범위·계약 #2 — 병합 완료](https://github.com/TheMostEvident/quant-trading-system/issues/2)
- [공통 기반 #3 — 설계 진행·실행 구현 미착수](https://github.com/TheMostEvident/quant-trading-system/issues/3)
