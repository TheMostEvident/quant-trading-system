# 공통 데이터 기반 검증 계획

작성일: 2026-11-04 (Asia/Seoul) · 관련 작업: [#3](https://github.com/TheMostEvident/quant-trading-system/issues/3)

## 상태

[설계](foundation-design.md)에 대한 **구현 후 검증 사양**이다. 아래 시나리오는 아직 저장 엔진·리플레이 코드로 실행하지 않았다. 문서 링크·항목·기대값의 정합성 점검과 런타임 검증을 구별한다.

## 1. 고정 fixture 규칙

- 모든 입력은 SYNTHETIC으로 표시하고 계좌/API/실제 주문 없이 재생한다.
- source namespace, 원천 native ID 유무, capture ID·epoch·counter, 수신 UTC/monotonic 간격을 명시한다.
- 원천 바이트·원천 표현과 기대 정규화 메시지는 별개로 보존한다.
- 사건 시각·수신 시각·처리 완료·소비자 수신·커밋 watermark·전달 cursor를 각각 고정한다.
- 비용/상품/계좌의 단위는 계약 v0.2를 사용한다. 실제 브로커 조건이라고 표시하지 않는다.

## 2. 시나리오 행렬

| ID | 입력·상황 | 구현 후 기대 결과 |
| --- | --- | --- |
| F01 | UTF-8 원문·공백·바이너리와 OCX callback 필드 표현 | 원래 표현별 바이트·크기·hash 복원; OCX를 wire capture로 표시하지 않음 |
| F02 | 같은 native ID·같은 바이트가 2회 도착 | capture 2회, blob 물리 저장 공유 가능, 논리 event/효과 1회 |
| F03 | 같은 바이트·서로 다른 native ID | capture/event 2회; hash만으로 제거하지 않음 |
| F04 | 같은 native ID·다른 내용 | 충돌/정정 분기, 임의 최신값 채택 금지 |
| F05 | 원천 native ID 없는 동일 내용 관측 2회 | 불확실성 기록·원천 보존, 완전 중복 제거 보장 금지 |
| F06 | 같은 fill_id 재전달·ACK 유실 | 원장 효과 1회·전달 이력 보존; 외부 주문 불명은 대조 |
| F07 | 메시지/outbox 트랜잭션 rollback | 소비자에게 전달되지 않음, 반쪽 메시지/outbox 없음 |
| F08 | raw commit 후 정규화 실패 | raw는 보존·오류 격리; 재처리 가능 |
| F09 | 문서/규칙의 늦은 소급 정정 | 정정 이용 가능 전 조회는 이전 버전 |
| F10 | 미리 알려진 미래 적용 규칙 | 적용 시각 전 이전 버전, 적용 시각부터 새 버전 |
| F11 | ready_at 이전 또는 consumer_seen_at 이전 조회 | 해당 메시지 제외 |
| F12 | event_at은 과거이나 커밋/전달은 decision cutoff 이후 | W 또는 D에서 제외; 사건 시각 정렬로 유입 금지 |
| F13 | 조회 중 새 commit·projection 갱신 | 하나의 읽기 snapshot/과거 watermark 결과 불변 |
| F14 | 최신 GAP/만료/철회, 이전 정상 자료 존재 | 이전 정상 버전으로 신규 진입을 허용하지 않음 |
| F15 | producer 재시작으로 monotonic offset 초기화 | 새 epoch 구분, 경과시간을 재시작 전과 잘못 차분하지 않음 |
| F16 | 큐/byte 한도 초과·디스크 부족 | 별도 health 상태·gap 기록, 신규 진입 제한, 정상 표시 금지 |
| F17 | 프로세스 중단 전후 미확인 송신/consumer cursor | 커밋된 outbox와 idempotency로 복구, 미확인 수신 구간은 GAP/UNKNOWN |
| F18 | 같은 manifest를 배속·호스트 시간 변경 후 재생 | 가상 시계·의미 trace 동일; 현재 wall clock 만료 적용 금지 |
| F19 | 재생에 뉴스/AI 작업 포함 | 저장 원문·응답만 사용; 새 네트워크 호출·실계좌 주문 없음 |
| F20 | WAL 버전/동기화 설정/로컬 경로 점검 | 실제 SQLite 빌드·설정 readback·data root를 기록; 조건 미충족 실행 차단 |
| F21 | 일관된 backup 생성·DB 복원·원천 digest 확인 | 원문·FK/참조·커밋 경계 일치; main DB 단독 복사를 backup으로 승인하지 않음 |
| F22 | 계약 v0.2 7종의 기록·조회·재생 | 단위·자산·승수·시간·계보·동일 계좌 버전 유지 |

## 3. 정정과 미래 적용의 기준 예시

모든 시각은 같은 날 UTC이며 해당 소비자의 실제 이용 가능 시각과 snapshot/cursor가 기록된 합성 예시다.

| 자료 | 이용 가능 시각 | 적용 시작 | 내용 |
| --- | --- | --- | --- |
| rule-v1 | 00:00:00Z | 00:00:00Z | 최초 규칙 |
| rule-v2-correction | 00:00:10Z | 00:00:00Z | 늦게 알려진 소급 정정 |
| rule-v3-future | 00:00:20Z | 00:01:00Z | 미리 공지한 미래 적용 |

| 조회 시각 | 기대 선택 | 이유 |
| --- | --- | --- |
| 00:00:09Z | rule-v1 | 정정 미관측 |
| 00:00:10Z | rule-v2-correction | 정정 관측·적용 시작 충족 |
| 00:00:30Z | rule-v2-correction | v3는 알려졌지만 아직 미적용 |
| 00:01:00Z | rule-v3-future | 적용 시작 포함 |
| 정책 expires_at과 같은 시각 | 신규 진입 불가 | 종료 경계는 exclusive |

ready_at=00:00:05Z, consumer_seen_at=00:00:08Z인 메시지는 00:00:07Z 조회에서 제외한다. event_at=00:00:01Z이어도 이 결과는 바뀌지 않는다. t 이전 시각을 가진 기록이라도 journal_seq가 당시 W 뒤이거나 consumer delivery가 D 뒤이면 제외한다.

## 4. 중복 기준 예시

| capture | native ID | 바이트 | 기대 논리 event |
| --- | --- | --- | --- |
| c1 | trade-10 | A | event-10 |
| c2 | trade-10 | A | event-10의 재전달 |
| c3 | trade-11 | A | event-11 |
| c4 | null | A | 별도 관측·동일성 미확정 |

동일 바이트 A는 물리 BLOB 한 개로 공유할 수 있지만 c1~c4의 도착 이력을 지우지 않는다. c1/c2의 중복 효과를 막는 근거는 확인된 native ID scope이지 payload hash가 아니다. native ID scope가 서로 다른 거래일/계좌/상품에서는 같은 문자열이어도 별개일 수 있다.

## 5. 실행 결과 기록 형식

| 항목 | 남길 내용 |
| --- | --- |
| 환경 | OS·프로세스 비트수·Python·실제 SQLite·의존성 lock·파일시스템 |
| 저장 프로필 | WAL/synchronous 실제 값·writer/reader/checkpoint 소유권 |
| 입력 | fixture/manifest hash·store/run ID·W/D·초기 상태 |
| 결과 | 기대/실제 capture/event/효과 수·trace/digest·대조 |
| 성능 | enqueue→commit→delivery 지연·queue high-water·busy/WAL/원천 크기 |
| 실패 | 프로세스 중단·rollback·ACK 유실·재시작·복구 전후 증거 |
| 판단 | 통과/실패/미실행·잔여 제한; 문서 검토와 코드 시험 구분 |

각 fixture와 실행 결과를 고정해 회귀 검증한다. 지연·처리량의 합격 숫자는 실제 피드와 전략 시간척도를 확인한 뒤 정하며 지표를 측정하지 않은 단계에서 초저지연·무손실·정확한 실체결을 주장하지 않는다.

## 6. 이번 문서 작업에서 확인한 것

- 설계 선택·데이터 키·시각·query cutoff·재생 사양과 F01~F22의 기대 결과를 정리했다.
- 작업 공간 메타데이터 조회에서는 Python 3.12.14 / SQLite 3.53.1이 확인되었다.
- 실제 SQLite DB 쓰기·저장 엔진·메시지 전달·리플레이·장애/복구 시험은 수행하지 않았다.
- 사용자 Windows·OCX 환경·실제 선물 피드·브로커 계좌도 확인하지 않았다.

따라서 위 시나리오의 통과 체크나 Issue #3의 구현 완료를 이번 PR의 문서 점검 결과로 대체하지 않는다.
