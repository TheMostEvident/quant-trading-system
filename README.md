# quant-trading-system

Research and development of a layered quantitative trading system.

현재는 **공통 기반과 Layer 1 수집·미시구조 지표의 구현·합성 검증 단계**다. 키움 callback 보존·정규화·품질 감시·재생에 OBI·관측 OFI·EMA·시간 변화율을 연결했다. Windows/OCX 실제 실행·피드 검증은 [Extra Issue 1 #33](https://github.com/TheMostEvident/quant-trading-system/issues/33)에서 진행한다.

## 실행

Python **3.12 이상**, 실제 로드되는 SQLite **3.51.3 이상**이 필요하다. Python 버전만으로 SQLite 버전이 충족되지는 않는다. 실행·시험에는 외부 Python 패키지가 필요 없다. 빌드 도구의 검증 버전은 [requirements-build.lock](requirements-build.lock)에 기록했다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m quant_system environment --probe-kiwoom
python -m unittest discover -s tests -v
quant-foundation demo --output-dir data/foundation-demo --config config/foundation.example.json
quant-foundation integrity data/foundation-demo/history.db
quant-foundation replay data/foundation-demo/manifest.json --run-id demo-replay
```

Windows PowerShell에서는 활성화 명령을 `.\.venv\Scripts\Activate.ps1`로 바꾼다. 코어와 실제 키움 OCX 어댑터의 실행 프로세스·비트수는 별도로 확인한다. `environment --probe-kiwoom`은 Windows 레지스트리 등록 정보만 확인하며 COM 활성화·로그인·이벤트 루프 성공을 의미하지 않는다.

데모는 빈 출력 폴더를 요구하며 SYNTHETIC 입력만 사용한다. 원천 2건 → 메시지 2건 → ACK 유실을 흉내 낸 전달 3회 → 업무 효과 2건, 과거/최신 버전 구별, 재생·백업 파일을 생성한다. 결과 폴더는 Git에서 제외된다. 실행 DB는 같은 호스트의 로컬 디스크에 둔다.

[검증 보고서](docs/foundation-verification-report.md)에 **45개 테스트**, F01~F22 대응, 설치·장애 주입·성능 측정 결과와 미검증 항목을 기록했다. Windows OCX·실제 국내 선물 피드 검증은 남아 있다.

## Layer 1 수집 경로

아래는 계좌·네트워크 없이 실행하는 합성 callback 예시다. 연결·거래 상태·호가·체결·재접속·stale을 처리하며 마지막 상태는 의도적으로 이용 불가가 된다.

```bash
quant-foundation market-ingest --input tests/fixtures/kiwoom-callbacks.synthetic.jsonl --database data/market.db --run-id market-demo --config config/kiwoom.synthetic.json --simulate-clock --manifest data/market.manifest.json
quant-foundation replay data/market.manifest.json --run-id market-replay
python tools/benchmark_market.py --events 200
```

같은 원천 callback을 재전달한 경우와 내용이 같은 별개 관측을 구별한다. BOOK는 상위 5단계의 독립 관측이며 TRADE/STATUS는 이전 book을 복사하지 않는다. 원천 sequence·native event ID·정확한 사건 UTC를 확인하지 못한 값은 null과 품질 플래그로 남긴다. 매매 신호·주문은 후속 Issue 범위다.

[수집기 실행·검증 안내](docs/kiwoom-feed.md)에 PR #34 당시 **83개 테스트(기존 45 + 수집 경로 38)**와 남은 실제 환경 검증을 정리했다. 코어는 stdlib만 사용하고 Windows bridge의 PyQt5 의존성은 별도 설치한다.

## 지표 계산과 독립 재계산

기본 `microstructure-v1`은 검증된 CONTINUOUS 입력에서 가중 OBI·관측 top-1 OFI·EMA·변화율·깊이/스프레드·체결 방향별 거래량을 계산한다. 설정은 [features.example.json](config/features.example.json)에 고정하고 run·manifest에 보존한다. 첫 OFI/변화율·0시간 차분·정보 부족은 null이며 GAP·stale·epoch/단위 변경은 계산 상태를 초기화한다.

```bash
quant-foundation market-ingest --input tests/fixtures/kiwoom-features.synthetic.jsonl --database data/features.db --run-id features-demo --config config/kiwoom.synthetic.json --feature-config config/features.example.json --simulate-clock --manifest data/features.manifest.json
quant-foundation verify-features data/features.manifest.json --run-id features-replay --speed 1
quant-foundation verify-features data/features.manifest.json --run-id features-replay --speed 1000
```

`verify-features`는 저장된 정규화 입력과 원천 수신 시계로 지표를 새로 계산하고 캐시와 대조한다. **125개 테스트(기존 83 + 지표 42)**, 손계산 표·정의·단위·시간창·재생 조건은 [미시구조 지표 안내](docs/microstructure-features.md)를 따른다. 계산식 검증과 실제 피드의 적합성 검증(#33)은 별개이며 현재 거래 신호나 주문을 생성하지 않는다.

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
- [구현 사용법·검증 결과](docs/foundation-verification-report.md)
- [국내 선물 수집 경로·Windows 실행·검증 결과](docs/kiwoom-feed.md)
- [OBI·OFI·EMA·시간 변화율·손계산·재계산 검증](docs/microstructure-features.md)
- [결정 기록](docs/decisions.md)
- [이전 현물 중심 계약 v0.1 — 이력 보존](docs/contracts-v0.1.md)

선물의 계약승수·증거금·정산·만기와 현물의 자산별 잔고를 구분한다. 모든 예시 수치·정책은 합성이며 실제 상품 사양·매매 추천·성능 추정치가 아니다.

## 작업 관리

- [전체 로드맵 #1](https://github.com/TheMostEvident/quant-trading-system/issues/1)
- [범위·계약 #2 — 병합 완료](https://github.com/TheMostEvident/quant-trading-system/issues/2)
- [공통 기반 #3 — 코어 완료, 환경 검증은 #33으로 이관](https://github.com/TheMostEvident/quant-trading-system/issues/3)
- [국내 선물 수집기 #4](https://github.com/TheMostEvident/quant-trading-system/issues/4)
- [미시구조 지표 #5](https://github.com/TheMostEvident/quant-trading-system/issues/5)
- [Extra Issue 1 #33 — Windows·OCX·실피드 검증](https://github.com/TheMostEvident/quant-trading-system/issues/33)
