# 설계 결정 기록

갱신일: 2026-11-05 (Asia/Seoul). 목표·우선순위에 대한 사용자 선택, 서비스 선호, 공식 기능 확인, 실제 연결 검증을 구별한다.

## 현재 결정

Issue #10의 초기 소스는 사용자 지시에 따라 **네이버 뉴스·KIND·KRX**로 한정한다 (2026-11-05). 추가 공급자 확장은 **Extra Issue 4 (#47)**로 분리한다. 기존 Extra Issue 1=#33, 2=#43, 3=#45의 범위·미검증 상태는 유지한다. 채택 소스와 실제 연결 검증은 다르며 구현/확인 범위는 [소스 수집 안내](source-collection.md)에 기록한다.

| ID | 항목 | 상태 | 근거·후속 조건 |
| --- | --- | --- | --- |
| D-001 | Layer 1 → Layer 3 → Layer 0 → Layer 2 | 사용자 확정 | 구축 순서 유지 |
| D-002 | 코드 작성 전에 단계·범위·계약 작성 | 선행 설계 완료 후 구현 승인 | PR #30·31 병합 뒤 사용자가 Issue #3 구현·검증 요청 |
| D-003 | 국내 주식 현물·국내 선물·해외 선물·크립토 현물 | 사용자 확정 (2026-11-04) | 최종 목표 범위; 모든 어댑터가 구현되었다는 의미 아님 |
| D-004 | 국내 선물부터 수집·모의매매 연결 | 사용자 확정 (2026-11-04) | 정확한 첫 계약·월물은 #4에서 결정 |
| D-005 | 키움증권·바이낸스를 우선 검토 | 사용자 선호 (2026-11-04) | 계좌 보유·API 권한·실제 접근을 확정한 것은 아님 |
| D-006 | 국내 선물은 키움 OpenAPI+ 공식 지원 상품부터 확인 | 공식 기능 확인에 따른 설계 선택 | Windows COM 경계; 개별주식선물 지원은 별도 검증 |
| D-007 | 해외 선물 OpenAPI-W·바이낸스 Spot은 별도 어댑터 | 설계 선택 | 국내 현물까지 REST 한 경로로 합치지 않음 |
| D-008 | seed와 고정 정책으로 Layer 1·3 독립 검증 | 유지 | Layer 0 소스 확정·Layer 2 연구가 초기 개발을 막지 않음 |
| D-009 | 선물 장부·현물 장부와 공통 위험 검사를 분리 | v0.2 설계 | 선물 명목가치를 현물 대금으로 차감하지 않음 |
| D-010 | 계약 v0.2: 자산·단위·승수·증거금·정산·만기·수수료 자산 | v0.2 설계 | 6개 핵심 메시지와 공통 AccountState |
| D-011 | 최종 확률모형·임계치·사이징·보유시간은 연구 후 선정 | 유지 | Phase 4–5와 실제 지연·비용 검증 |
| D-012 | 코어 Python·SQLite, bridge 언어·IPC·클라우드 공급자 | 코어 구현, 실제 bridge 후속 | #3 코어는 stdlib; Windows/OCX·IPC transport는 별도 검증 |

## 이전 가정의 대체

| 이전 v0.1 가정 | 현재 처리 |
| --- | --- |
| 국내 현물 매수부터 검증 | 국내 선물 우선으로 대체 |
| KIS 첫 어댑터 우선 검토 | 사용자 선호에 따라 키움 경로 우선; KIS는 대안 이력 |
| 현물 양수 보유 수량·주 단위·단일 통화 비용 | 상품별 signed 계약·소수 기준자산·자산별 비용·증거금 모델로 확장 |
| 6개 메시지만의 계좌 상태 참조 | 공통 AccountState를 명시한 v0.2로 대체 |

[contracts-v0.1.md](contracts-v0.1.md)는 이전 제안의 이력이며 현재 구현 기준은 [contracts-v0.2.md](contracts-v0.2.md)이다. 이전 사용자 미응답 상태를 현재 미확정 목표로 계속 표시하지 않는다.

## 남아 있는 구현 전 확인

- 키움 계좌·서비스·모의 환경, 첫 국내 선물 상품·월물, Windows 실행·OCX 호환 비트수.
- 국내 개별주식선물의 실제 API 지원과 각 경로의 시각·호가·체결·잔고 필드.
- 해외 선물 시장·계약과 크립토 현물 거래쌍, 국내 선물 이후의 연결 순서.
- 상품별 수수료·세금·증거금·정산·만기·달력과 실제 데이터의 이용 가능 시각.
- Layer 0 전체 Info Source, 최종 모형·최적 정책과 실계좌 운용 전환.

위 사항은 설계 문서를 만드는 데 필요한 선택과 실제 연결 시에 확인할 사실을 구분하기 위해 남긴다. 예시값이나 다른 상품의 규칙으로 빈칸을 자동 채우지 않는다.

## 계약 변경·검토 규칙

- 의미·단위·필수성이 바뀐 이번 개정은 0.1.0 → 0.2.0으로 표시한다.
- 구버전을 묵시적으로 새 상품에 적용하지 않는다. 변환 시 누락 항목을 확인하고 지원 불가 기능은 차단한다.
- 상품별 원장 규칙과 원천 메시지를 보존한다. 공급자 필드·COM 핸들을 공통 계약에 직접 노출하지 않는다.
- 사용자 목표와 API 경로 선택이 반영되었으므로 PR은 검토 가능한 설계로 전환한다. 병합과 실제 코드 작성은 별도 작업이다.

## Issue #3 공통 기반 설계 (2026-11-04)

PR #30과 Issue #2의 범위·계약, PR #31의 공통 기반 설계는 main에 병합되었다. 다음 설계를 바탕으로 사용자 요청에 따라 이식 가능한 코어를 구현·검증했다. 실제 Windows/OCX 환경은 남아 있다.

| ID | 선택 | 상태·근거 |
| --- | --- | --- |
| D-013 | 초기 로컬 SQLite BLOB + append-only journal, 단일 writer | 설계 선택; 동일 DB 트랜잭션으로 raw·메시지·outbox 일관성 단순화 |
| D-014 | WAL + synchronous=FULL, SQLite 3.51.3 이상 | 공식 조건에 따른 초기 프로필; 실제 설치·설정·복구 검증 필요 |
| D-015 | 코어 CPython 3.12 계열과 Windows COM bridge의 프로세스 분리 | 코어 후보 선정; bridge 비트수·호환 기술은 실제 OCX 확인 후 결정 |
| D-016 | hash는 바이트 식별, 논리 중복은 확인된 source identity scope | 동일 호가 내용의 정상 반복과 같은 사건의 재전송 구별 |
| D-017 | 소비자 시점 조회는 t + 커밋 watermark W + 전달 cursor D | source event time·최신 projection만으로 과거 상태 구성 금지 |
| D-018 | 재생 manifest·가상 시계·저장된 AI 응답 사용 | 같은 입력 trace 재현; 새로운 외부 응답·실계좌 주문 배제 |
| D-019 | Issue #3은 설계 3-A와 구현 3-B~E를 구분 | 설계 PR #31 이후 구현 승인; 외부 환경 검증 없이 전체 Issue 종료하지 않음 |
| D-020 | 코어는 외부 runtime/test 패키지 없이 Python stdlib 사용 | unittest 45개·wheel 설치·오프라인 데모 검증; 빌드 도구 버전 별도 기록 |
| D-021 | 정규화 실패·원천 충돌·재시작·포화는 명시적 health 기록 | 복구에는 별도 reconciliation evidence 요구; 소비자는 live gate도 확인 |
| D-022 | payload 참조도 동일 run·이미 저장됨·ready cutoff 검증 | 저장되지 않은 규칙·미래 계좌/상품 참조의 조기 사용 차단 |
| D-023 | 재생은 전달별 health 이력과 참조 view 보존 | 마지막 health로 과거 모든 입력을 다시 표시하지 않음 |

상세 규칙은 [공통 기반 설계](foundation-design.md), 기대 결과와 구현 후 검증은 [검증 계획](foundation-validation-plan.md)을 따른다.

## Issue #4 수집 구현·검증 분리 (2026-11-04)

| ID | 선택 | 근거·경계 |
| --- | --- | --- |
| D-024 | Windows/OCX 현장 검증을 Extra Issue 1 #33으로 분리 | #3 코어는 PR #32로 병합·종료; 미실행 환경을 통과로 바꾸지 않음 |
| D-025 | OCX GUI callback → bounded 메모리 큐 → 로컬 append-only JSONL → 별도 코어 tailer | GUI 스레드에서 필드 추출만 하고 파일 fsync·SQLite 처리 분리; spool/follow와 health sidecar로 같은 호스트의 프로세스 경계 구성 |
| D-026 | 첫 파서는 국내 지수선물 top-5 callback만 처리 | guide 1.7 §8.5/8.6을 구현 근거로 삼음; 실제 설치 명세·첫 코드/월물·구독 방식은 #33 확인 |
| D-027 | 원천 사건 UTC·native ID·공급자 sequence를 추정하지 않음 | HHMMSS는 raw에 보존; trading date는 명시적 프로필; 자정/역행은 epoch 재확인 전 차단 |
| D-028 | 모의 로그인 확인 후 시세 함수만 호출 | controller/Qt port에 주문·계좌·잔고 API 없음; 인증은 사용자 PC 공식 창에서 수행 |
| D-029 | 실제 프로필의 상품·거래 상태 검증 플래그는 기본 false | 주식의 장 상태 코드와 선물의 상태 범위를 동일시하지 않음; 설치 명세/현장 증거 전 market_status=UNKNOWN |
| D-030 | 재시작은 새 run과 연결·상태·모든 선택 상품의 새 full snapshot으로 시작 | 과거 정상 book을 부활시키지 않음; 공급자 무손실/완전 순서를 보장하는 resync는 아님 |

구현과 시험·실행 조건은 [수집 안내](kiwoom-feed.md), 테스트 근거는 [기계 판독 결과](verification/kiwoom-20261104.json)를 따른다.

## Issue #5 미시구조 지표 (2026-11-04)

| ID | 선택 | 근거·경계 |
| --- | --- | --- |
| D-031 | microstructure-v1을 v0.2 봉투의 선택적 feature extension으로 구현 | 기존 7종 계약 유지; 설정 artifact·단위·clock·상태/coverage 명시 |
| D-032 | 가중 OBI와 가격 이동을 반영하는 관측 top-1 OFI를 구분 | snapshots 사이의 flow이며 모든 거래소 이벤트를 복원했다는 뜻이 아님 |
| D-033 | 시간 EMA와 event EMA·관측당/시간당 변화율을 구분 | 첫 rate와 0시간 rate는 null; 미래 관측·wall clock을 수학 계산에 사용하지 않음 |
| D-034 | 특징을 준비하고 메시지 커밋 후에만 상태 반영 | publish 실패 시 EMA/window 상태가 미커밋 메시지를 참조하지 않음; 계산 중 stale이 되면 결과 폐기 |
| D-035 | producer clock·spec/date/unit 변경 또는 freshness gap에서 새 계산 구간 시작 | 이전 정상 자료로 GAP·stale을 메우지 않음; source freshness가 계산 연속성의 상한 |
| D-036 | bounded time-window overflow에서 partial sum을 null로 표시 | 누락이 시간창 밖으로 나갈 때까지 coverage=false; raw는 저장 |
| D-037 | replay에서 저장된 정규화 입력과 raw clock으로 독립 재계산 | producer journal order·ready cutoff·계보 검사; consumer 재전달을 다시 계산하지 않음 |

공식 식의 근거·프로젝트 선택·손계산 표·125개 검증과 잔여 #33 조건은 [미시구조 지표](microstructure-features.md)를 따른다.

## Issue #6 고정 정책 신호 (2026-11-04)

| ID | 선택 | 근거·경계 |
| --- | --- | --- |
| D-038 | static-obi-v1 명시적 STATIC extension만 실행 | 기존 0.2 봉투 유지; Layer 2·예측 regime을 가장하지 않음 |
| D-039 | 신호는 signed 총 목표이며 실제 보유를 변경하지 않음 | prepare/commit과 중복 결정 구분; OMS·체결/예약 원장은 #7 이후 |
| D-040 | N회·원천 경과시간·유지 히스테리시스·선택적 진입 slope를 분리 | 중복/처리 지연은 지속성 증거가 아니며 slope 부호만으로 기본 청산하지 않음 |
| D-041 | 만료 후 신규 증가를 금지하고 기존 감소·보유시간·유지 문턱을 계속 평가 | 최초 체결 기산점은 정책·주문 갱신으로 초기화하지 않음; 반전은 새 version의 flat 확인 뒤 새 streak |
| D-042 | capability·단위·명목가치·예약·현재 상태에 묶인 증거금 quote 검사 | actual provider가 없으면 추정 진입하지 않음; 가용 증거금에서 예약을 중복 차감하지 않음 |
| D-043 | 현물은 CASH/보유 재고 감소만 지원 | CONTRACT-only 지표를 현물로 바꾸지 않음; 신규 진입·현물 OBI 유지는 후속 |
| D-044 | EVENT_EMA α=1의 결과를 현재 OBI로 정확히 설정 | cancellation 오차가 유지 문턱 equality를 깨는 회귀를 P16으로 검증; 과거 캐시는 원래 code와 보존 |
| D-045 | CLI는 가용 시점이 선언된 오프라인 bundle 평가 | 실제 t/W/D 소비자 연결·시계 매핑·원장/브로커 인증은 후속; #33 실피드 검증을 대체하지 않음 |

정의·경계·198개 시험과 설치 결과는 [고정 정책](static-policy.md)과 [검증 JSON](verification/policy-20261104.json)을 따른다.

## Issue #7 주문 상태·원장 (2026-11-04)

| ID | 선택 | 근거·경계 |
| --- | --- | --- |
| D-046 | raw-first 뒤 OMS receipt·projection·command queue를 한 transaction에 commit | 단일 writer/WAL/FULL 재사용; 미커밋 강제 종료에서 예약/command 유출 금지 |
| D-047 | 전송 전에 command를 한 번 claim하고 결과 불명 시 재전송하지 않음 | 외부 exactly-once 보장이 아니며 broker 대조 후 복구 |
| D-048 | 취소 ACK 뒤에도 예약을 유지하고 전체 snapshot/watermark로 종료 대조 | 지연 체결·취소 경합·과도한 재청산/반전을 방지; 실제 complete/quiesced provider는 후속 검증 |
| D-049 | fill 식별자와 전체 상세만으로 수량/자산에 반영 | 누적 숫자로 미상 체결을 만들지 않음; 역순/누락·conflict는 보존·대조 |
| D-050 | synthetic-fifo-v1의 exact lot·현금/증거금/정산/미정산손익 분리 | 평균 표시값으로 현금을 계산하지 않음; 실제 브로커 회계/반올림·증거금은 미확인 |
| D-051 | 자산별 비용·현물 순재고와 CASH 원장을 분리 | BASE_ASSET 소수 비용·rebate 보존, 암묵 FX·선물 숏/margin을 현물에 적용하지 않음 |
| D-052 | signal 연결은 선언한 t/W/D 입력과 최신 유효 policy/book을 요구 | 과거 정상 정책·호가 부활 차단; direct TARGET은 합성 bundle API |
| D-053 | 알려진 BUST만 반대 분개 delta와 영향을 받는 lot/정산 재계산으로 처리 | 과거 receipt는 불변; 다른 정정은 격리하고 재대조 |
| D-054 | 브로커 전송과 자동 체결 생성은 구현하지 않음 | OMS/원장 코어 검증과 #8 모의 집행·#33 실제 환경을 분리 |

정의·상태·한계·282개 검증은 [OMS 안내](oms-ledger.md)와 [검증 JSON](verification/oms-20261104.json)을 따른다.
