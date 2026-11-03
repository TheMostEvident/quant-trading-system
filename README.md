# quant-trading-system

Research and development of a layered quantitative trading system.

현재 단계는 **연구 설계**이다. 이 저장소의 첫 산출물은 구축 범위와 Layer 간 메시지 규격이며, 수집기·전략·주문 엔진은 아직 구현하지 않았다.

## 구축 순서

| Phase | 대상 | 범위 |
| --- | --- | --- |
| 0 | 공통 준비 | 범위·입출력 계약, 이후 원천 보존·재생 기반 |
| 1 | Layer 1 | 호가·체결, 미시구조 특징, 고정 정책, 모의 집행 |
| 2 | Layer 3 | 공시·뉴스, 근거 추출, 이벤트·기업 연결, Jev 판단 |
| 3 | Layer 0 | Info Source 확정, 배경 자료, 시점별 후보군 |
| 4 | Layer 2 연구 | 금융수학·확률제어 학습, 모형·정책·사이징 연구 |
| 5 | Layer 2 구현 | 상태 추정, 동적 정책, 전체 통합 검증 |

Layer 1·3은 임시 종목 목록과 고정 정책으로 먼저 검증한다. Layer 2 구현은 Layer 0 구축과 선행 연구를 마친 뒤 진행한다.

## 설계 문서

- [범위·구조·진행 조건](docs/scope.md)
- [공통 메시지 계약 v0.1](docs/contracts-v0.1.md)
- [시장·상품·API 및 실행 환경 비교](docs/source-evaluation.md)
- [결정 기록과 미결정 항목](docs/decisions.md)

## 작업 관리

- [전체 로드맵 #1](https://github.com/TheMostEvident/quant-trading-system/issues/1)
- [첫 작업: 범위와 입출력 규격 #2](https://github.com/TheMostEvident/quant-trading-system/issues/2)

현물 매수 중심은 v0.1 검증을 위한 작업 가정이다. 사용자 확정 사항, 작업 가정, 실제 API 연결로 확인해야 할 사항은 결정 기록에서 구별한다. 예시 메시지와 정책 수치는 모두 합성 자료이며 거래 추천이나 성능 추정치가 아니다.
