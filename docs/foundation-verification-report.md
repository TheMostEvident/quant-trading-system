# 공통 기반 구현·검증 보고서

2026-11-04 (Asia/Seoul) · [Issue #3](https://github.com/TheMostEvident/quant-trading-system/issues/3) · 기반 main: `04796938cb58b2f652333468cfefe72b3205aae6`

원천 저장·최소 한 번 전달·시점 조회·오프라인 재생을 구현하고 **45개 unittest를 통과**했다. 실제 Windows OCX·브로커·국내 선물 피드는 미검증이다. 따라서 Issue #3은 외부 환경 확인 항목을 남겨 열어 둔다.

## 구현 범위

| 경로 | 제공하는 기능 |
| --- | --- |
| `src/quant_system/contracts/messages.py` | 계약 v0.2.0 7종의 필수 구조·시간·decimal 단위·호가·체결·확률·정책 주요 불변식 검증 |
| `src/quant_system/foundation/store.py` | SQLite BLOB/journal·capture·불변 참조·중복/충돌·version·outbox·ACK·업무 효과 receipt·boundary·조회·백업·checkpoint |
| `src/quant_system/foundation/buffer.py` | item/byte 한도 큐·별도 health 경로·포화 계측·명시적 복구 |
| `src/quant_system/foundation/replay.py` | 의존 참조를 포함한 manifest·digest·전달 순서·가상 시계·전달별 health·새 run 참조 view |
| `src/quant_system/foundation/environment.py` | 런타임·WAL 버전 조건·단일 writer lease·환경 보고·Windows 레지스트리 probe |
| `src/quant_system/demo.py`, `config/` | API 연결 없이 실행하는 합성 데모·실제 적용하는 크기/큐 한도 |
| `tools/benchmark_foundation.py` | 합성 입력의 enqueue→commit→delivery 지연·WAL·checkpoint 측정 |

raw는 먼저 별도 커밋하고 검증된 메시지·identity·version·outbox는 하나의 트랜잭션으로 커밋한다. payload hash는 물리 바이트 공유용이며, 논리 중복은 확인된 source/stream/scope/native ID로 구별한다. native ID가 없으면 별개 관측을 보존한다. 정규화 버전은 journal과 manifest에 남긴다.

시점 조회는 저장된 `Boundary(store_id, run_id, consumer_id, t, W, D, decision_id)`를 요구한다. payload 참조도 이미 저장된 같은 run의 객체이고 `ready_at` 이하여야 한다. 의사결정의 `input_refs`는 호출자가 명시한 당시 visible 메시지의 부분집합이며, 생략하면 빈 목록이다. 단순히 보였다는 사실을 실제 사용으로 표시하지 않는다.

정책 version의 revision·provider epoch·적용 시작은 본문과 일치해야 하며 만료를 연장할 수 없다. 최신 GAP/만료/철회를 선택한 뒤 이전 정상 버전을 되살리지 않는다. 이 기반의 `usable()`은 저장 상태 검사이며 향후 주문 엔진의 전체 위험 한도 검사를 대신하지 않는다.

## 실행 환경과 설치

| 항목 | 실제 결과 |
| --- | --- |
| OS / 프로세스 | Linux / 64-bit |
| Python / 실제 SQLite | 3.12.14 / 3.53.1 |
| DB | 로컬 작업 경로, WAL, synchronous=FULL(2), FK ON, busy timeout 5000ms |
| writer / reader | OS lifetime lease 한 writer; 독립 readonly WAL snapshot 허용 |
| checkpoint | 자동 checkpoint 해제; 소유 writer의 명시적 `checkpoint()` |
| runtime / test 의존성 | Python 표준라이브러리만 사용; unittest |
| 빌드 도구 | setuptools 84.1.0 / wheel 0.48.0 / pip 25.0.1; 버전 pin 별도 기록 |
| 설치 검증 | editable 설치와 wheel 빌드 후 별도의 기본 venv에 `--no-index --no-deps` 설치 |
| 독립성 확인 | PYTHONPATH 제거; import가 검증 venv의 site-packages를 가리키는 것 확인 |
| 키움 probe | `UNAVAILABLE_ON_THIS_PLATFORM`; 계좌·COM 활성화·event loop 모두 미시험 |

최종 wheel을 설치한 환경에서 아래 시험을 수행했다. 실제 Python/SQLite 빌드가 최소 조건을 만족하지 않으면 저장소 개방을 거절한다. `requirements-build.lock`은 빌드 도구 버전 pin이며 OS·Python·SQLite 바이너리의 hash lock은 아니다.

```bash
python -m unittest discover -s tests -v
quant-foundation environment --probe-kiwoom
quant-foundation demo --output-dir data/foundation-demo --config config/foundation.example.json
quant-foundation integrity data/foundation-demo/history.db
quant-foundation replay data/foundation-demo/manifest.json --run-id demo-replay
python tools/benchmark_foundation.py --events 200
```

## 시나리오 대응

아래 함수는 모두 [tests/test_foundation.py](../tests/test_foundation.py)의 `FoundationTests`에 있다. F20은 코드 경로만 통과했고 실제 Windows 검증은 남아 있다.

| ID | 시험 함수의 `test_` 뒤 이름 | 결과·확인 |
| --- | --- | --- |
| F01 | `F01_raw_bytes_and_callback_representation` | 통과: 공백·바이너리·callback 표현 그대로 복원 |
| F02 | `F02_same_source_event_two_captures_one_logical_event` | 통과: capture 2 / blob 1 / message·outbox 1 |
| F03 | `F03_identical_content_different_events_stay_distinct` | 통과: 같은 바이트, 다른 native ID는 event 2 |
| F04 | `F04_source_identity_collision_is_audited` | 통과: 충돌·health 기록; 기존 메시지 유지 |
| F05 | `F05_no_native_id_does_not_deduplicate_by_hash` | 통과: ID 불명 관측 2건 보존 |
| F06 | `F06_lost_ack_and_duplicate_fill_effect_are_idempotent` | 통과: 재전달해도 확인된 fill 업무 효과 1회 |
| F07 | `F07_transaction_failure_cannot_publish_half_an_outbox` | 통과: commit 직전 예외로 메시지/outbox 함께 rollback |
| F08 | `F08_normalization_failure_keeps_raw_and_can_retry` | 통과: raw 유지·오류 기록·재처리 |
| F09 | `F09_late_retroactive_correction_cannot_rewrite_past` | 통과: 정정의 이용 가능 시각 이전에는 구버전 |
| F10 | `F10_announced_future_rule_waits_until_effective_time` | 통과: 미래 적용 시작 전에는 이전 규칙 |
| F11 | `F11_ready_and_consumer_arrival_both_constrain_visibility` | 통과: ready와 consumer seen 모두 제한 |
| F12 | `F12_watermark_and_delivery_cursor_exclude_backdated_inputs` | 통과: 사후 입력·전달은 W/D로 제외; 변조 boundary 거부 |
| F13 | `F13_reader_snapshot_and_old_boundary_do_not_change` | 통과: 새 commit 이후에도 snapshot·과거 boundary 불변 |
| F14 | `F14_gap_expiry_and_tombstone_do_not_resurrect_old_policy` | 통과: GAP·만료·철회로 과거 정상값 부활 차단 |
| F15 | `F15_restart_epochs_reset_monotonic_basis_and_require_recovery` | 통과: epoch 재설정·대조 전 health 중단 |
| F16 | `F16_bounded_queue_and_actual_sqlite_full_are_not_silent` | 통과: 큐 포화와 실제 SQLITE_FULL; 무음 성공 없음 |
| F17 | `F17_process_exit_during_transaction_and_lost_ack_recover` | 통과: 자식의 `os._exit(77)` 이후 commit만 보존·재전달 |
| F18 | `F18_manifest_virtual_clock_is_repeatable_independent_of_speed` | 통과: 배속별 같은 trace·간격·참조 매핑·변조 탐지 |
| F19 | `F19_replay_uses_stored_ai_result_without_network` | 통과: socket/sleep 금지 상태에서 저장된 AI 판단 재생 |
| F20 | `F20_runtime_profile_and_path_guards` | 코드 통과: WAL/FULL·버전·UNC guard; Windows OCX 미실행 |
| F21 | `F21_online_backup_restore_and_digest_detection` | 통과: online backup·readonly 복원·BLOB 변조 탐지 |
| F22 | `F22_all_seven_contracts_roundtrip_through_store_and_replay` | 통과: 7종 저장/전달/재생; account/position 참조·단위 유지 |

추가 23개 시험으로 append-only·writer 중복/경로 별칭·ID scope·decimal 정밀도·owner 전환·run 간 계보·순행 monotonic·미래 장애와 과거 health 분리·미전달 효과 차단·품질 충돌·무관 DB 보존·정규화 실패 gate·합성/관측 혼합 표시·정책 파라미터·불명 증거금/보유 상태·본문 참조 시점·전달별 health·CLI 전체 실행/덮어쓰기 차단·checkpoint 소유권·실제 입력 선택·version 일치를 확인했다.

F16은 DB page 한도(`PRAGMA max_page_count`)로 SQLite 엔진이 SQLITE_FULL을 발생하게 한 시험이다. 호스트 물리 디스크를 가득 채우거나 전원을 차단한 시험은 아니다. F17은 별도 OS 프로세스가 미커밋 트랜잭션 중 정리 없이 종료되는 경로를 시험했다.

## 데모와 소규모 측정

데모는 capture/message 각 2건, 전달 3회, 업무 효과 2건을 기록했다. 과거 boundary는 `book-1`, 이후 boundary는 `book-2`를 반환했다. manifest의 배속 1·1000에서 같은 trace digest를 얻었고 원 DB·online backup의 무결성을 확인했다. 다른 실행은 store ID·경로가 달라 manifest hash도 달라질 수 있다. 결정성 비교는 **동일 manifest·동일 target run**을 기준으로 한다.

아래는 synthetic snapshot 200건을 한 건씩 enqueue→raw commit→message commit→delivery→ACK한 단일 측정이다. 독립 venv의 설치된 패키지로 수행했다.

| 구간 | p95 (ms) | p99 (ms) |
| --- | ---: | ---: |
| enqueue → raw commit | 4.559 | 8.414 |
| 정규화 결과 검증·commit | 5.660 | 9.592 |
| delivery commit | 4.482 | 8.094 |
| ACK commit | 5.536 | 8.920 |
| enqueue → delivery | 11.980 | 23.949 |

최종 설치 빌드의 측정은 200건 / 1.267초, 약 157.9건/초였다. 앞선 소규모 측정과도 지연 차이가 있어 환경 변동을 포함한 탐색치로 취급한다. queue high-water는 1건/62바이트, drop·busy 실패 0, raw 합계 12,292바이트, checkpoint 전 WAL 24,802,432바이트였다. PASSIVE checkpoint는 busy=0, 6,020/6,020 frame 완료였다. PASSIVE는 파일을 truncate하지 않으므로 WAL 파일 크기 감소를 의미하지 않는다.

이번 측정은 burst·동시 원격 입력·장시간 reader·실제 broker callback을 포함하지 않는다. 임시 저장 환경의 fsync와 실제 물리 디스크의 전원 손실 내구성도 동일하다고 볼 수 없다. 수치는 매매 전략의 지연 목표나 HFT 처리 능력의 합격선이 아니다. 실제 피드에서 WAL 한도·checkpoint 주기·backpressure·지연 목표를 정해야 한다. 원시 측정값·fixture hash·데모 digest는 [JSON 결과](verification/foundation-20261104.json)에 보존했다.

## 다음 작업에 남긴 경계

- Windows/OCX: 등록 탐지만 구현했다. 실제 모듈·호환 비트수·COM 활성화·event loop·계좌/모의 접근은 사용자 환경에서 검증해야 한다.
- 어댑터: callback→Capture 변환·실제 원천 ID scope·FID/TR·IPC transport·재접속/GAP 대조는 [Issue #4](https://github.com/TheMostEvident/quant-trading-system/issues/4) 범위다. 코어의 구조 검증은 원시 broker parser가 아니다.
- 저장 운영: UNC·mapped Windows network drive·symlink 별칭은 검사하되 POSIX NFS·동기화 폴더·hardlink의 로컬성은 자동 판별하지 않는다. caller가 로컬 경로를 보장한다. checkpoint API는 제공하지만 운영 scheduler·WAL/디스크 강제 상한 감시는 후속 통합이다.
- 상품·주문: InstrumentSpec/증거금 규칙은 불변 참조로 보존한다. 실제 상품별 lot/tick·정산·증거금 계산기·OMS·외부 exactly-once 주문은 구현 범위가 아니다. 업무 효과 키가 올바른지는 도메인 소비자가 책임진다.
- 재생: 저장된 입력 trace와 참조를 복원한다. 새 AI 호출·미래 전략·거래소 체결 결정성을 증명하지 않는다. replay 자체에는 네트워크·broker·실시간 대기 hook이 없다.

Layer 1 → Layer 3 → Layer 0 → Layer 2 연구/구현의 순서는 유지한다. 이 PR을 병합한 뒤 #4의 코드 작업을 진행할 수 있으며 실제 연결에 앞서 남은 Windows/OCX gate를 확인해야 한다.
