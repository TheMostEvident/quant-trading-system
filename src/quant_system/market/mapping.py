"""OpenAPI+ guide 1.7, sections 8.5/8.6; installed KOA verification pending #33."""

GUIDE = "https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.7.pdf"
BOOK_TYPE = "선물호가잔량"
TRADE_TYPE = "선물시세"
STATUS_TYPE = "장시작시간"
DEPTH = 5
BOOK_FIDS = (21,) + tuple(fid + level for level in range(DEPTH) for fid in (41, 51, 61, 71))
TRADE_FIDS = (20, 10, 15)
# A status mapping for the selected futures session must be supplied and
# independently verified. Do not assume equity-market codes describe futures.
STATUS_FIDS = (215,)
FIDS = {BOOK_TYPE: BOOK_FIDS, TRADE_TYPE: TRADE_FIDS, STATUS_TYPE: STATUS_FIDS}
