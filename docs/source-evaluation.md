# 상품별 API와 실행 환경: 국내 선물 우선

확인·갱신: 2026-11-04 (Asia/Seoul). 사용자 선택은 국내 주식 현물·국내 선물·해외 선물·크립토 현물이며 첫 연결은 국내 선물이다. 키움증권·바이낸스는 우선 검토 서비스이다. 공식 자료 확인과 사용자 계좌 인증·수신·주문 시험은 구별한다.

## 1. 상품별 연결 후보

| 상품 | 경로 | 공식 안내에서 확인한 것 | 현재 판단 |
| --- | --- | --- | --- |
| 국내 선물 | 키움 OpenAPI+ | 코스피200 선물과 Windows COM/OCX 방식 | 최초 수집·모의 검증의 우선 경로 |
| 국내 개별주식선물 | 키움의 상품별 실제 명세·서비스 확인 필요 | 위 안내만으로 지원을 단정할 수 없음 | 목표 범위에 유지; 기능 확인 전 지원 표시 금지 |
| 해외 선물 | 키움 해외파생 OpenAPI-W | 별도 해외선물 서비스·Windows COM 경로 | 별도 어댑터로 후속 검증 |
| 국내 주식 현물 | 키움 OpenAPI+ / REST | 상품과 실행 방식이 다른 서비스 | 현물 연결 때 기능·운영 조건으로 선택 |
| 크립토 현물 | Binance Spot API | 공식 REST/WebSocket·자산/주문 필터·사용자 이벤트 문서 | 현물 전용 어댑터 후속 검증 |
| 대체 증권 경로 | KIS | 이전에 비교한 상품별 예제·REST/WebSocket 경로 | 사용자 선호의 우선 경로에서 제외; 필요 시 대안 검토 |

키움 REST의 멀티 OS 안내를 국내 선물 지원 근거로 사용하지 않는다. 현재 공식 상품 안내는 REST와 OpenAPI+ 및 해외파생 경로를 구분한다. API 기능 추가나 변경은 연결 시 다시 확인한다.

## 2. 최초 국내 선물 연결의 확인 목록

- 사용자에게 이용 가능한 선물 계좌·서비스 신청·모의 환경과 실제 제공 상품.
- 첫 계약의 코드·월물·기초자산·호가 규칙·거래승수·거래일/세션·마지막 거래·정산 방식.
- 시세·호가·체결 통보·주문 조회·잔고 대조의 TR/FID와 시각·호가 깊이·집계/누락 정보.
- Windows COM/OCX 호환 환경·비트수·이벤트 루프·로그인·복구와 작업자 프로세스 경계.
- 실제 조회/구독 한도와 피드 지연. 숫자를 확인하지 않은 채 구독 수·레이턴시를 약속하지 않음.
- 매수/매도의 신규·청산 의미, 네팅 방식, 증거금·주문 예약·정산 통보의 제공 범위.

최초 후보는 코스피200 선물 계열이다. 표준/미니 등 세부 상품·월물·수량·레버리지 수준은 이 문서에서 임의 선택하지 않는다.

## 3. 바이낸스 현물의 별도 요구

- 거래쌍의 base/quote 자산과 PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL/NOTIONAL 등 적용 필터를 버전으로 관리한다.
- 수수료를 금액 하나와 가정한 통화로 축약하지 않는다. 사용자 이벤트의 commission asset을 보존한다.
- diff-depth를 사용할 경우 REST 스냅숏과 업데이트 ID 범위에 따라 로컬 호가를 동기화하고 누락 후 재동기화한다.
- 공개 시세·사용자 체결·계좌 잔고 경로와 내부 모의·거래소 테스트 환경을 구별한다.
- 실제 거래쌍·계좌 접근·제공 기능 확인 전 자동매매 지원 완료로 표시하지 않는다.

USDT와 USD의 가치를 자동으로 같게 두지 않는다. 위 현물 요건은 크립토 선물이나 무기한 계약의 추가를 의미하지 않는다.

## 4. 공통 배포 방향

Windows 전용 키움 어댑터는 표준화한 메시지로 공통 엔진과 통신한다. Layer 3의 클라우드 작업이 COM 이벤트나 주문 처리를 막지 않도록 한다. Python/C#/C++ 등 연결 기술과 공통 엔진의 언어·DB·IPC는 #3의 환경 검증 후 정한다.

국내 현물·선물·해외 시장·크립토의 세션과 보유시간·수신 간격을 동일하게 가정하지 않는다. 실제 피드의 스냅숏·집계·시각 정밀도에 따라 OBI·관측 OFI의 의미와 한계를 기록한다.

## 5. 공식 근거

- [키움 API 상품별 안내](https://www.kiwoom.com/h/customer/download/VChannelHts4View?tab=3)
- [키움 OpenAPI+ 안내](https://www.kiwoom.com/m/customer/download/VOpenApiInfoView)
- [키움 해외파생 OpenAPI-W 안내](https://www3.kiwoom.com/h/customer/download/VFofuopOpenApiInfoView?dummyVal=0)
- [키움 REST 안내](https://openapi.kiwoom.com/intro/serviceInfo)
- [Binance 공식 Spot API 문서](https://github.com/binance/binance-spot-api-docs)
- [Binance Spot filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)
- [Binance WebSocket 및 로컬 호가 동기화](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)
- [Binance 사용자 이벤트·commission asset](https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md)
- [KIS 공식 샘플 — 대안 비교 이력](https://github.com/koreainvestment/open-trading-api)
- [KRX 배경자료 API 목록 — Layer 0에서 별도 확정](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)

샘플·공식 문서가 있다는 사실만으로 실제 계좌 권한·모의 환경·전체 상품을 확인한 것으로 처리하지 않는다.
