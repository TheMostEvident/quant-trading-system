# 공통 데이터 기반 설계 v0.1

작성일: 2026-11-04 (Asia/Seoul) · 대상: [Issue #3](https://github.com/TheMostEvident/quant-trading-system/issues/3) · 메시지 계약: [v0.2](contracts-v0.2.md)

## 1. 상태와 범위

이 문서는 원천 저장·식별·시점 조회·전달·재생을 구현할 기준이다. 현재 산출물은 설계이며 저장소 코드·DB·수집기·실행 환경을 구축했다는 의미가 아니다. Issue #3의 실제 저장·재생·장애 검증은 후속 구현 항목으로 남긴다.

국내 선물을 먼저 검증하고 이후 국내 현물·해외 선물·크립토 현물에 같은 데이터 의미를 적용한다. 상품별 단위·장부·승수·증거금·정산은 계약 v0.2를 유지한다. Layer 0·2가 없어도 seed와 고정 정책, 저장된 합성 입력으로 기반을 검증할 수 있어야 한다.

## 2. 초기 기술 선택

| 항목 | 초기 설계 선택 | 이유·경계 |
| --- | --- | --- |
| 공통 런타임 | CPython 3.12 계열의 별도 프로세스 | 64-bit 코어 후보; 정확한 배포 빌드·의존성은 구현 검증 후 잠금 |
| 키움 국내 선물 | 별도 Windows COM/OCX 어댑터 | 비트수·이벤트 루프·로그인·호환 언어는 실제 모듈 확인; 코어의 Python 버전과 동일하다고 가정하지 않음 |
| 쓰기 저장소 | 로컬 파일시스템의 SQLite, 단일 writer | 원천 BLOB·저널·메시지·outbox의 원자적 트랜잭션을 단순화 |
| 원천 데이터 | SQLite BLOB에 수신 표현 그대로 | 텍스트 정리·재직렬화 전 바이트와 표현 형식·크기·SHA-256 보존 |
| 읽기 | 별도 read connection의 짧은 snapshot transaction | 여러 조회 사이에 관측 상태가 바뀌는 문제 방지 |
| 전달 | 로컬 프로세스 간 bounded 큐 + durable outbox | COM callback의 계산/DB 대기 분리; 전달은 최소 한 번 |
| 재생 | 같은 소비자 계약, 가상 시계, 고정 manifest | 새 외부 데이터·Jev 호출·브로커 주문을 실행 경로에서 배제 |
| 초기 배치 | 키움 adapter와 코어/DB는 같은 Windows 호스트의 별도 프로세스 | 네트워크 공유 DB 없이 시작; 코어의 OS 이식성 유지 |
| Layer 3·후속 원격 입력 | 별도 worker가 메시지로 제출 | 원격 프로세스가 SQLite 파일을 직접 열지 않음 |

현재는 파일별 원문 + DB 인덱스의 이중 커밋이나 분산 로그를 도입하지 않는다. 큰 문서·이벤트 폭증이 지연·저장 요구를 넘기면 immutable blob 저장소/서버 DB로 교체할 수 있는 BlobStore·Journal 경계를 둔다. 교체를 현재 구현 기능으로 표시하지 않는다.

### SQLite 운영 조건

- WAL은 같은 호스트의 로컬 디스크에서만 사용한다. 네트워크 드라이브·동기화 폴더를 실행 중인 DB 경로로 두지 않는다.
- 초기 내구성 프로필은 synchronous=FULL이다. 실제 적용값을 다시 읽어 실행 메타데이터에 남긴다. 성능을 이유로 NORMAL/OFF로 조용히 낮추지 않는다.
- writer와 checkpoint의 소유권을 한 프로세스에서 직렬 관리하고, 긴 읽기·WAL 성장·busy·checkpoint 지연을 관측한다.
- WAL 사용 빌드는 SQLite 3.51.3 이상을 초기 조건으로 둔다. 공식 WAL-reset 수정 여부와 실제 로드한 SQLite 버전을 확인한다. Python 버전만 보고 SQLite 빌드를 추정하지 않는다.
- 운영 백업은 SQLite가 보장하는 일관된 방식으로 생성하고 복원·무결성을 검사한다. 실행 중인 main DB 파일만 복사한 결과를 완전한 백업으로 표시하지 않는다.
- 전원 손실·OS·디스크의 동작까지 문서만으로 보장하지 않는다. 실제 환경에서 장애·복구 시험을 통과한 프로필만 실행 가능으로 표시한다.

설계 시 확인한 작업 공간은 Python 3.12.14 / SQLite 3.53.1이다. 이는 사용자 Windows·OCX 환경 확인이나 저장 엔진 구현 시험 결과가 아니다.

## 3. 저장소 구성과 원천 크기

| 예정 경로 | 책임 | 현재 상태 |
| --- | --- | --- |
| docs/ | 범위·계약·설계·결정·검증 계획 | 문서 작성 |
| src/quant_system/contracts/ | 계약 검증·단위·버전 | 구현 예정 |
| src/quant_system/foundation/ | 저널·blob·outbox·시계·시점 조회·재생 | 구현 예정 |
| adapters/kiwoom/ | Windows 원천 callback·연결 변환 | 실제 환경 확인 후 구현 |
| tests/fixtures/ | 합성 raw·메시지·정정·지연·장애 사례 | 검증 계획만 정의 |
| tests/foundation/ | 저장·재생·장애 검증 | 구현 예정 |
| config/ | 비밀값 없는 실행 프로필 | 구현 예정 |
| research/ | 모형·정책 학습 결과 | Layer 2 연구 단계 |
| 외부 runtime data root | DB·raw·로그·실행 결과·백업 | 사용자 환경에서 별도 생성 |

프로덕션 자료·계좌정보·API 키·실행 DB·WAL을 Git에 넣지 않는다. fixture는 합성/실수집 여부와 사용 가능한 범위를 표시한다. 논리 경로만 정의하며 이번 문서 작업에서 소스 디렉터리나 실행 설정을 생성하지 않는다.

큰 BLOB·입력 폭증은 writer를 막을 수 있다. max_raw_bytes·queue item/byte 한도·batch 크기/시간·WAL/디스크 한도를 실행 프로필에 명시하고 초과를 계측한다. 값은 throughput·p95/p99 지연·손실 조건을 측정한 뒤 선택하며 이 문서가 초저지연을 보장하지 않는다.

## 4. 데이터 모델

| 논리 테이블/기록 | 키·주요 내용 | 변경 정책 |
| --- | --- | --- |
| stores / runs | store_id, run_id, 실행 모드·계약·코드/설정·원천·시계/비용/상품 버전 | 생성 후 내용 불변; 상태 변화는 journal |
| producer_epochs | producer_id·epoch·시작 UTC·monotonic 원점·시계 품질 | 재시작마다 새 epoch |
| raw_blobs | raw_sha256, byte_length, payload BLOB | 동일 바이트의 물리 저장만 공유 |
| journal | store_id·ingest_seq, capture_id, run_id·원천·유형·시간·media_type/representation·blob·계보 | append-only의 권위 기록 |
| messages | message_id, schema/type, journal 참조, 최초 직렬화 바이트와 hash | 계약 v0.2 7종·ID 충돌 검증 |
| event_identities | source_namespace·identity_scope·native_id → 논리 event/message | 실제로 확인된 원천 ID 규칙만 사용 |
| object_versions | object_key·version_id·owner/epoch·revision·effective_from·expires_at·supersedes·tombstone | 정정·철회도 새 버전 |
| delivery_events | consumer_id·delivery_seq·message_id·seen_at·시계/epoch·결과 | 전달·재전송·ACK 이력 추가 |
| decision_boundaries | consumer·decision_id·decision_time·journal watermark·delivery cursor·실제 input_refs | 당시 읽기 범위와 사용 입력 불변 |
| outbox / cursors | 송신 대기·재시도·현재 consumer cursor | 재생 가능한 운영 projection; 변경 이력은 journal |
| replay_manifests | 입력 범위·digest·설정/상품/모델 응답 버전·seed·정렬/시계 | 재생 사양 불변 |

journal의 ingest_seq는 단일 writer가 커밋한 증가 순서다. DB 수명 내 커밋 ID 재사용을 막는 AUTOINCREMENT 의미를 사용하며 연속 번호를 보장하지 않는다. rollback·수집 누락을 단순히 번호 공백으로 추정하지 않는다. 다른 store_id나 run_id의 순번을 글로벌 시각처럼 비교하지 않는다. ingest_seq는 저장소의 정확한 정수이며 프로세스 간 직렬화에서 부동소수점으로 반올림하지 않는다.

원문·호가·체결·계좌·정책·이벤트를 같은 원천 종류로 축약하지 않는다. 작은 projection을 갱신하더라도 과거 권위 기록을 수정해 원장을 다시 쓴 것으로 만들지 않는다. append-only는 애플리케이션 저장 정책이며 관리자에 대한 변조 불가능성 보증이 아니다.

### Raw 표현과 hash

- HTTP/PDF/파일은 수신 바이트를 보존한다. 압축·인코딩 해제본은 별도 파생 레코드이며 원본 hash와 연결한다.
- COM/OCX는 실제로 받은 callback 필드·이벤트 이름·공급자 심볼·원래 값·순서를 표현한 capture를 보존한다. 공급자가 제공하지 않은 네트워크 wire bytes를 보존했다고 표시하지 않는다.
- raw_sha256은 바이트 식별용이다. Unicode 정규화·HTML 정리·숫자 형변환 전에 계산한다.
- 정규화 메시지는 producer가 최초 직렬화한 바이트를 저장하고 재전송 시 그대로 사용한다. message hash가 원천 hash를 대체하지 않는다.
- 설정·원천 변환·프롬프트·저장된 AI 응답은 독립 버전과 hash로 참조한다.

## 5. 중복과 사건 식별

| 관측 | 저장/처리 |
| --- | --- |
| 같은 실제 원천 ID·같은 내용의 재전송 | raw 도착 2회는 보존, 논리 event·메시지·효과는 1회 |
| 같은 바이트·다른 실제 사건 ID | raw blob은 공유 가능, 도착·사건·특징 갱신은 서로 다름 |
| 같은 원천 ID·다른 내용 | 충돌/정정 여부 격리·기록; 자동으로 앞/뒤 하나를 선택하지 않음 |
| 공급자 사건 ID가 없음 | capture마다 별도 수신 ID. 바이트 hash만으로 같은 사건이라고 제거하지 않음 |
| 같은 fill_id 재통보 | 원장 효과 1회; raw/전달 이력은 모두 보존 |
| 문서 재인용 | 문서 식별과 Layer 3의 사건 병합을 구별; 독립 근거를 임의 증가시키지 않음 |

capture_id는 producer_id/epoch/local counter로 부여하고 재전송할 이미 저장된 capture에는 같은 ID를 사용한다. 공급자가 재발송한 원천은 별도 capture가 될 수 있다. 이 로컬 ID가 공급자 사건의 정확한 동일성을 증명하지는 않는다.

identity_scope는 실제 공급자 명세에 맞춰 거래소·계좌·상품·거래일·세션 등을 포함한다. 예를 들어 계좌/상품별 trade ID를 모든 시장에서 글로벌 ID로 가정하지 않는다. native ID가 없는 피드의 완전한 중복 제거는 보장하지 않으며 불확실성과 피드 상태를 기록한다. 계좌 효과는 확실한 체결 식별 또는 검증된 누적/잔고 대조 없이 capture counter를 가상 체결 ID로 삼아 누적하지 않는다.

## 6. 커밋·전달·복구 순서

1. callback은 최소 capture와 원래 수신 UTC/monotonic 간격을 큐에 넣는다. COM callback에서 무거운 파싱·Jev·DB 대기를 수행하지 않는다.
2. writer는 raw BLOB과 RAW journal을 같은 트랜잭션으로 저장한다. 실패하면 후속 메시지를 소비자에게 공개하지 않는다.
3. normalizer는 커밋된 raw만 읽는다. 원천 ID·스키마·단위를 검증하고 메시지·사건 식별·정정 버전·outbox를 하나의 트랜잭션으로 커밋한다.
4. dispatcher는 커밋된 outbox에서 읽고 소비자에게 전달한다. 전송·재전송·ACK 이력을 남긴다.
5. consumer는 전달 ID·원천 도메인 ID를 확인한다. 실제 의사결정은 snapshot watermark와 전달 cursor·input_refs를 남긴다.
6. 주문·원장 등 효과는 입력/의사결정과 함께 확인 가능한 상태 전이·outbox를 저장한 뒤 수행한다. 외부 주문의 응답이 불명확하면 재전송만으로 해결하지 않고 대조한다. 실제 주문 상태 구현은 #7이다.
7. 재시작은 새 producer epoch, 미확인 outbox·cursor·마지막 정상 관측을 대조하고 gap/계좌 상태가 해소되기 전 신규 노출을 늘리지 않는다.

최소 한 번 전달 + 도메인별 idempotency가 목표다. 외부 브로커와 DB를 하나의 트랜잭션으로 묶은 exactly-once를 약속하지 않는다. 기록 전 프로세스가 죽은 수신 구간은 복구 불가능할 수 있으며, 보장하지 못한 구간을 GAP/UNKNOWN으로 남긴다.

큐가 가득 차거나 디스크가 불충분하면 조용히 샘플을 버리고 정상 처리로 표시하지 않는다. 큐 포화와 별도로 전달 가능한 health 경로/상태를 두어 즉시 신규 진입을 제한하고 누락·재동기화 사실을 기록한다. 지연을 숨기기 위해 원래 수신 시각을 writer 처리 시각으로 바꾸지 않는다.

## 7. 시간 필드와 두 시간축

| 필드 | 의미 | 금지되는 대체 |
| --- | --- | --- |
| event_at | 공급자 사건 시각 | 이 시각만으로 정보가 이용 가능했다고 판단 |
| published_at / publication_precision | 문서 발행 시각·정밀도 | 일자 정보에 임의 초 추가 |
| received_at | 수집 계층 최초 관측 | 처리 완료 시각으로 덮어쓰기 |
| ready_at | 해당 producer의 결과 처리 완료 | 실제 DB commit 완료 시각이라고 단정 |
| effective_at / valid_from | 규칙·정책의 적용 시작 | 아직 전달되지 않은 변경의 조기 적용 |
| expires_at | 적용 종료, end exclusive | 만료 후 신규 진입 허용 |
| consumer_seen_at | 해당 consumer가 받은 시각 | 다른 worker의 처리 시각으로 추정 |
| producer_epoch + monotonic offset | 같은 epoch의 수신 간격 | 재시작 전후 offset을 같은 시계로 차분 |
| decision_time + watermark/cursor | 그 소비자가 실제 읽은 지식 범위 | 현재 최신 상태를 과거 상태로 사용 |

원천/생산자 간 UTC 오차는 clock quality와 측정 방법을 기록한다. 미확인 원격 시계를 정밀 동기화로 표시하지 않는다. UTC 역행·큰 보정·epoch 불명은 clock quality 저하로 기록하고 경과시간을 UTC 차분으로 대체하지 않는다. 전달 순서와 수신 간격은 해당 consumer/epoch 기준으로 재현하며 허용 오차와 부적격 처리 기준을 구현 프로필에서 정한다.

## 8. As-of 조회 규칙

입력은 store_id, run/consumer, 조회 시각 t, 읽기 snapshot의 커밋 watermark W, 소비자 전달 cursor D이다. 실제 의사결정을 재현할 때는 저장된 decision boundary를 사용한다.

후보는 W 이하의 커밋된 기록, D까지 해당 소비자가 실제 받은 기록이며 max(ready_at, consumer_seen_at) <= t를 만족해야 한다. 현재 DB에 있더라도 커밋 순번이 W 뒤이거나 나중에 전달된 입력은 제외한다. DB 행 삽입 시각을 정확한 commit 완료 시각으로 위장하지 않는다.

- 버전 자료는 **알려진 시점**과 **적용 시점**을 함께 만족하는 owner/epoch/revision을 선택한다.
- 나중에 들어온 소급 정정은 그 정정이 알려지기 전의 조회 결과를 바꾸지 않는다.
- 미리 공지된 미래 적용 규칙은 valid_from 전에는 이전 적용 규칙을 유지한다.
- 철회/tombstone가 적용되면 오래된 정상 버전으로 조용히 되돌아가지 않는다.
- 최신 GAP/UNKNOWN/만료·노후 상태를 무시하고 과거의 정상 호가·정책을 신규 진입에 사용하지 않는다.
- 가격·호가 선택을 위해 event_at으로 자료를 미래에서 당겨 재정렬하지 않는다.
- 단일 읽기 트랜잭션 안에서 일관된 snapshot을 사용한다. 조회 도중 새 commit을 일부 테이블에만 반영하지 않는다.

원래 consumer 전달 이력이 없는 자료는 producer-view 연구 재생으로 표시한다. 이를 실제 전략이 당시 관측한 입력의 정확한 재현이라고 주장하지 않는다.

## 9. 재생 manifest와 결정성

| manifest 항목 | 필수 내용 |
| --- | --- |
| 입력 식별 | store_id, source_run_id, immutable snapshot/backup ID, 커밋/전달 범위 |
| 무결성 | raw·메시지·참조 객체의 digest와 계약 버전 |
| 실행 정의 | 코드·런타임·스키마/정규화/특징 버전·고정 설정 |
| 시간 | journal + consumer delivery 정렬, 원래 monotonic 간격, 가상 시계 시작·배속 |
| 자산·계좌 | InstrumentSpec·달력·증거금·정산·seed·고정 정책·초기 상태 |
| 비정형 결과 | 원문·문단 근거·질문/모델·저장된 Jev 응답 버전 |
| 난수 | 사용하는 각 모의 구성의 알고리즘·seed·호출 순서 |
| 비교 기준 | 의미 있는 입력/신호/의도/체결/상태 trace, 출력 digest와 제외할 운영 메타데이터 |

순서는 저장된 전달 순서와 ingest tie-breaker로 고정한다. wall clock·실행 속도·스레드 완료 순서·디렉터리 열거 순서에 맡기지 않는다. 실제 실행에서 동시에 완료된 비동기 작업의 관측 순서를 저장하고 재생한다.

외부 API·Jev를 다시 호출하지 않고 저장된 결과를 재사용한다. 가상 시계의 정책 만료·수신 간격을 사용한다. 원천 봉투는 그대로 보존한다. 재생 run의 소비자 view에서 source_message_id → replay_message_id와 run namespace를 결정적으로 매핑하고 모든 계보·정책·계좌 참조를 일관되게 변환한다. 실제 run을 재생 run으로 덮어쓰지 않는다.

새 run ID·호스트·실제 처리 시간 등 운영 메타데이터와 경제적 의미가 있는 결정 trace를 구별하며 비교에서 제외할 필드를 미리 정한다. side·수량·가격·비용 자산·시점·한도/정책 판단을 임의 제외해 결과 차이를 숨기지 않는다.

같은 manifest에서 입력·의미 있는 trace가 재현되어야 한다. 이는 기록된 시스템의 재현성 목표이며 미래 시장 가격·브로커 체결·새 AI 호출의 결정성 보증이 아니다.

## 10. 구현 순서와 완료 판단

| 단계 | 구현할 것 | 필수 확인 |
| --- | --- | --- |
| 3-A 설계 | 모델·키·시점·조회·전달·재생·검증 기준 | 현재 문서 작업 |
| 3-B 최소 환경 | 실제 runtime·SQLite·임시 data root·버전 잠금 | 실제 실행 보고서 |
| 3-C 저장/식별 | raw BLOB·journal·중복·정정·outbox | 원문 복원·트랜잭션·충돌·전달 검증 |
| 3-D 조회/재생 | snapshot cutoff·버전 선택·manifest·가상 시계 | [검증 계획](foundation-validation-plan.md)의 기대 결과 |
| 3-E 복구 | 큐 포화·재시작·commit/ACK 경합·백업/복원 | 장애 주입·불변식 확인 |

앞서 정한 코드 작성 보류에 따라 3-A만 이번 PR의 완료 범위다. 3-B~E와 Issue #3의 실행 검증은 미완료다. 실제 구현 없이 테스트 통과나 Issue 전체 완료로 표시하지 않는다.

## 공식 근거

- [SQLite WAL 및 같은 호스트/버전 조건](https://sqlite.org/wal.html)
- [SQLite 읽기 snapshot과 격리](https://sqlite.org/isolation.html)
- [SQLite synchronous 의미](https://sqlite.org/pragma.html#pragma_synchronous)
- [SQLite AUTOINCREMENT 의미](https://sqlite.org/autoinc.html)
- [Python 3.12 sqlite3](https://docs.python.org/3.12/library/sqlite3.html)

트랜잭션·로그·중복·manifest·consumer cutoff는 위 도구의 기능 위에서 정한 프로젝트 설계이다. 공식 문서가 이 시스템 전체의 결정성·실제 피드 무손실을 검증했다는 의미가 아니다.
