# Issue #10: 네이버 뉴스·KIND·KRX 수집

사용자 결정: 2026-11-05 (Asia/Seoul). 초기 소스는 이 세 곳으로 한정한다. DART·SEC EDGAR 등 추가 공급자의 채택·어댑터 확장은 [Extra Issue 4 #47](https://github.com/TheMostEvident/quant-trading-system/issues/47)에서 진행한다. 기존 Extra Issue 2 #43의 선물 자료 검증은 원래 범위로 유지한다.

## 구현과 실제 접근 상태

| 소스 ID | 이번 구현 | 현재 검증 범위 | 남아 있는 확인 |
| --- | --- | --- | --- |
| NAVER_NEWS | 공식 검색 JSON 요청, 제목·요약·링크·제공 시각, 제한된 offset 순회 | 공식 명세·합성 응답·오류/재시도·저장/재검증 | 사용자 앱 인증으로 실제 접근, 한도·이용 조건과 표본 대조 |
| KIND_RSS | 공식 화면에서 복사한 RSS URL의 GET, RSS 목록 메타데이터 | 공식 화면의 RSS 제공 확인·합성 RSS·인코딩/오류·저장/재검증 | 실제 RSS URL과 응답 스키마/시간/윈도우 확인. 테스트 URL은 가짜이며 실제 endpoint가 아니다. |
| KRX_PAGE | 공개 KRX 페이지의 원시 HTML 스냅샷 보존 | 공개 페이지의 존재·합성 HTML·저장/재검증 | 이 환경에서는 직접 접근 시 403을 관측했다. 실제 본문 확인·동적 목록·게시물별 링크/첨부 추출은 미완료다. |

KRX는 KIND와 별도 소스 ID를 쓴다. 기본 URL은 공식 Market 홈페이지 `https://open.krx.co.kr/main/main.jsp`이며 페이지 전체를 하나의 `HTML_PAGE_SNAPSHOT`으로 보존한다. 게시물 목록을 추출했다는 뜻이 아니다. HTML 200이 로그인/차단 화면일 수도 있으므로 `content_verified=false`, `raw_only=true`, `metadata_normalized=false`로 남긴다. KRX의 Data Marketplace 일별 통계 API를 뉴스·공시 API로 대체하지 않는다. 상품 마스터·배경 통계 전체 구축은 Layer 0 범위다.

KIND의 RSS endpoint를 추측해 기본값으로 넣지 않았다. 공식 오늘의공시 화면에서 사용자가 복사한 URL을 설정한다. 지원 호스트는 `kind.krx.co.kr`, 경로는 `/disclosure/`이고 현재 지원하는 공개 query 이름 외에는 거부한다. 실제 URL에 다른 필드가 필요하면 명세/응답을 확인한 뒤 지원 범위를 갱신한다. RSS가 없거나 형식이 바뀌면 HTML을 성공 응답으로 처리하지 않는다.

이 구현만으로 Issue #10의 실제 접근 완료 조건을 충족했다고 표시하지 않는다. #11의 HTML·XML·PDF 구조 파싱·근거 위치, #12의 기업/이벤트 연결·정정 판단, #13의 Jev 판단, #14의 비동기 운영은 후속이다. 주문·계좌 API나 새 EventPacket 발행은 추가하지 않았다.

## 공통 수집 규격과 영속 저장

소스별 요청/파서는 분리하고 결과만 공통 규격으로 보존한다. 코어 Python/SQLite의 기존 단일 writer·WAL·FULL·로컬 디스크 조건을 재사용한다.

| 기록 | 의미 |
| --- | --- |
| Capture | HTTP entity 바이트와 소스/URL·수신 시각·단조 시계·표현·SHA-256. TLS/HTTP 헤더를 포함하는 wire 전체가 아니다. 오류 응답도 우선 commit한다. |
| source-fetch-v1 | 공개 요청·cursor·설정, HTTP 상태, 오류, raw 참조, 정규화 hash, 페이지 범위, 문서 observation 참조 |
| source-document-v1 | 소스/항목 key·내용 variant, 원문 링크·제목·요약/목록/페이지 구분, 공급 시각의 원문·의미·정밀도, raw 참조, 수신/준비 시각·첫 관측 참조 |
| source-collection-report-v1 | 실행별 예산·중단 사유·반복/변경 관측 수·원천 범위·연결 확인의 한계 |

원시 응답을 먼저 commit하고, 한 응답의 문서 artifacts·fetch artifact·항목 identity index를 같은 다음 transaction에 commit한다. 정규화 실패 또는 transaction 중단 때 원시는 남고 미커밋 문서는 노출되지 않는다. 표와 artifacts는 불변이며 통합 integrity/hash 검사와 `verify-source-collection`으로 원시 바이트를 다시 파싱해 대조한다. 후자는 라이브 접근/데이터 완전성 인증이 아니다.

모든 관측은 저장한다. 같은 항목/내용은 `REPEATED_VARIANT`, 같은 항목의 새 내용은 `NEW_VARIANT`로 구분한다. 새 variant를 법적 정정 공시나 새 사건으로 판단하지 않는다. 네이버는 반환 링크, KIND는 **feed URL + guid 또는 link**, KRX는 페이지 URL 범위의 key를 쓴다. 서로 다른 RSS URL·언론사·공시를 하나의 사건으로 합치지 않는다.

`first_received_at`은 **같은 DB·소스 identity 범위·data_origin 내 최초로 commit된 관측의 수신 시각**이다. 전 세계 최초 보도/수집 시각이 아니며 SYNTHETIC 표본이 OBSERVED의 최초 관측을 선점하지 못한다. 과거 run의 첫 관측 참조는 계보이며 현재 소비자의 이용 가능성을 보장하지 않는다. `ready_at`은 정규화 transaction 직전의 로컬 논리 시각이다. 실제 커밋/이용 가능성은 journal watermark와 함께 판단해야 한다. 이 단계는 source artifacts를 주문 소비자에 전달하지 않는다.

네이버 `pubDate`는 명세의 **네이버에 제공된 시간 또는 원문이 제공된 시간**이라는 의미를 보존한다. 출판사의 최초 발행 시각으로 단정하지 않는다. RSS `pubDate`의 실제 의미는 미확인으로 남긴다. 명시적 offset이 있는 RFC 날짜만 UTC로 정규화하며 날짜만 있으면 `DATE`, 시각/시간대가 없거나 잘못됐으면 `UNKNOWN`과 null이다. 자정이나 수집 시각을 발행 시각으로 만들어 넣지 않는다.

검색의 `description`은 요약이고 RSS description은 목록 메타데이터다. 링크는 참조로만 보존하며 외부 언론사·DART 링크를 자동 방문하지 않는다. 참조는 HTTP/HTTPS일 수 있지만 수집 요청은 검증한 HTTPS 호스트로만 나간다.

## 호출·범위·오류

- GET만 허용한다. 네이버 요청은 공식 endpoint·query/display/start/sort=date로 고정하며 인증은 런타임 환경 변수로만 받는다. 요청/저장 artifact/로그에 인증 header를 넣지 않는다.
- 자동 redirect를 거부해 인증의 다른 호스트 전달과 수집 범위 변경을 막는다. 오류 문자열에는 proxy/server가 인증 값을 반사할 수 있어 원래 exception 문자열을 기록하지 않는다. 응답에 인증 문자열이 그대로 반사되면 바이트를 보존하지 않고 `OMITTED_SENSITIVE_RESPONSE`로 명시한다.
- 응답 크기는 기본 4 MiB, 상한 16 MiB다. 초과 시 prefix만 저장하고 `HTTP_ENTITY_PREFIX`/`RESPONSE_TOO_LARGE`로 표시하며 정규화하지 않는다. DTD/entity·지원하지 않는 XML encoding, 압축 응답과 HTML 오류 페이지를 RSS로 받지 않는다.
- 기본 페이지 2개·요청 4회·시도 2회·최소 대기 1초다. 설정 상한은 페이지 10개·요청 30회·시도 3회·응답 대기 30초이며 408/429 및 일부 5xx/transport 오류만 재시도한다. Retry-After가 60초를 넘으면 조기 재시도 대신 실행을 중단한다. 계정 전체 일일 사용량을 이 실행의 예산으로 인증하지 않는다.
- 네이버 start의 명세 한도 1000 안에서만 순회한다. 검색 결과는 이동하는 offset 창이어서 반복·변경·누락이 가능하다. 짧은 페이지인데 total이 남아 있으면 `INCONSISTENT_SHORT_PAGE`로 끝내고 정상 수집으로 표시하지 않는다.
- KIND는 최근 RSS 창 하나이며 과거 페이지·전체 공시 coverage를 보장하지 않는다. KRX는 원시 페이지 하나다. 모든 결과는 `dataset_complete=false`다. 범위 제한과 오류를 확인한 뒤 필요한 수집 기간/빈도를 별도로 설계한다.

`collection_ok`는 설정된 실행에서 entity를 저장하고 해당 범위의 처리를 끝냈다는 뜻이다. `RAW_PAGE_ONLY`나 검색/페이지/요청 한도 종료도 보고서에 남는다. 전체 자료·정시 수집·정정 추적의 완전성이 아니다. `http_success_observed`는 실제 모드에서 HTTP 200과 해당 처리를 관측했는지만 나타낸다. `live_access_verified`는 이 구현이 자동 발급하는 인증이 아니며 항상 false다. 실제 endpoint·권한·응답/시각/이용 조건 대조 결과를 별도 기록해야 한다.

## 오프라인 실행

프로젝트 루트에서 설치한 코어 Python으로 실행한다. 아래 표본은 전부 합성이다. 기본 실행은 외부 네트워크를 호출하지 않으며 `--fixture`와 `--live` 중 하나를 반드시 지정한다. 같은 DB를 재사용할 수 있지만 매번 새 run ID가 필요하다.

```powershell
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config tests/fixtures/sources/naver-config.json --fixture tests/fixtures/sources/naver-responses.json --database data/sources-offline.sqlite --run-id naver-synthetic-1 --report data/naver-synthetic-1.json
.\.venv-core\Scripts\python.exe -m quant_system verify-source-collection data/sources-offline.sqlite --run-id naver-synthetic-1
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config tests/fixtures/sources/kind-config.json --fixture tests/fixtures/sources/kind-responses.json --database data/sources-offline.sqlite --run-id kind-synthetic-1
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config config/sources/krx-page.example.json --fixture tests/fixtures/sources/krx-responses.json --database data/sources-offline.sqlite --run-id krx-synthetic-1
```

네이버 표본은 2페이지·4관측 중 같은 내용의 재관측 1개·같은 항목의 새 내용 1개를 포함한다. 따라서 total=4 도달을 서로 다른 기사 4개/완전 수집으로 해석하지 않는다. KIND는 2개 공시 메타데이터 중 날짜만 있는 표본을 포함한다. KRX는 원시 페이지 1개다.

## 실제 접근 확인 절차

네이버 앱을 등록하고 공식 검색 API 사용 권한을 확인한다. 키는 사용자 PC의 `NAVER_CLIENT_ID`·`NAVER_CLIENT_SECRET` 환경 변수에만 설정한다. KIND는 공식 오늘의공시 화면의 RSS URL을 `kind-rss.template.json`의 복사본에 넣는다. 토큰·계좌·인증 정보를 채팅/Issue/저장소에 제출하지 않는다. 코어는 Windows OCX/PyQt 없이도 실행된다.

```powershell
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config config/sources/naver-news.example.json --live --database data/sources-observed.sqlite --run-id "naver-$Stamp" --report "data/naver-$Stamp.json"
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config config/sources/kind-rss.local.json --live --database data/sources-observed.sqlite --run-id "kind-$Stamp" --report "data/kind-$Stamp.json"
.\.venv-core\Scripts\python.exe -m quant_system source-collect --config config/sources/krx-page.example.json --live --database data/sources-observed.sqlite --run-id "krx-$Stamp" --report "data/krx-$Stamp.json"
```

보고서의 오류·중단 사유·coverage를 먼저 확인하고 raw와 공급자 화면/명세를 대조한다. KRX 페이지가 차단/동적 껍데기이면 원시 보존만으로 지원 완료 처리하지 않는다. 실제 KIND 응답이 지원 schema와 다르면 해당 원시 자료를 기준으로 parser를 조정한 뒤 검증한다. 실제 키와 endpoint 접근이 없는 이 작업 환경의 합성 시험으로 이 절차를 통과 처리하지 않았다.

## 공식 근거와 검증

- [네이버 뉴스 검색 명세](https://developers.naver.com/docs/serviceapi/search/news/news.md): GET JSON/XML, 인증 headers, display/start 범위, 검색 요약·제공 시간의 의미.
- [KIND 오늘의공시](https://kind.krx.co.kr/disclosure/todaydisclosure.do?method=searchTodayDisclosureMain): 공식 RSS 진입점. 이번 합성 XML/URL은 실제 서비스 응답을 복사한 표본이 아니다.
- [KRX Market](https://open.krx.co.kr/main/main.jsp): 보도자료·행사소식 페이지. 직접 접근이 성공한 것으로 표시하지 않는다.
- [KRX Open API 서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)과 [이용방법](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO003.jsp): 통계 정보·인증키/서비스 신청이 별도이며 이번 페이지 수집과 구분한다.

검증: 새 37개와 기존 384개를 합한 **421개 Linux 시험**. raw 선행 저장·transaction rollback·관측 identity·합성/실제 namespace·offset 한계·오류/재시도 예산·인증 비저장/redirect·크기 제한·RSS entity 거부·원시 바이트 재파싱을 포함한다. 설치본/CLI 결과와 미검증 상태는 [검증 JSON](verification/sources-20261105.json)에 기록한다. 기존 실환경 검증 #33/#43/#45와 추가 공급자 확장 #47은 별도이며 이 구현으로 완료되지 않는다.
