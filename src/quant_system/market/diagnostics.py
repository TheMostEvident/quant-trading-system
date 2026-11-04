"""Bounded diagnostic metadata; no account access or market-state inference."""

from collections import Counter


INITIAL_RQ = "qts-initial-quote"
INITIAL_TR = "opt50001"
INITIAL_SCREEN = "9100"
INITIAL_FIELDS = ("종목코드", "종목명", "현재가", "체결시간")


class EventObservation:
    def __init__(self, *, max_types=32):
        self.max_types = max_types
        self.by_type = Counter()
        self.total = 0
        self.filtered_type = 0
        self.filtered_code = 0
        self.after_tr_request = 0
        self.after_tr_response = 0

    def record(self, real_type, *, tr_requested=False, tr_received=False):
        self.total += 1
        name = real_type if isinstance(real_type, str) and 0 < len(real_type) <= 64 else "<INVALID_TYPE>"
        if name not in self.by_type and len(self.by_type) >= self.max_types:
            name = "<OTHER_TYPES>"
        self.by_type[name] += 1
        self.after_tr_request += bool(tr_requested)
        self.after_tr_response += bool(tr_received)

    def summary(self):
        return {"total_before_filter": self.total, "by_type": dict(self.by_type),
                "filtered_unknown_type": self.filtered_type, "filtered_unselected_code": self.filtered_code,
                "after_initial_tr_request": self.after_tr_request, "after_initial_tr_response": self.after_tr_response}


def collection_diagnosis(*, mock, registered, observations, accepted, initial_tr):
    if not mock:
        return "MOCK_LOGIN_NOT_CONFIRMED"
    if not registered:
        return "REGISTRATION_NOT_CONFIRMED"
    if initial_tr["enabled"] and not initial_tr["response_count"]:
        return "INITIAL_TR_RESPONSE_NOT_OBSERVED"
    if (initial_tr.get("response") or {}).get("error_code") not in (None, 0):
        return "INITIAL_TR_RESPONSE_ERROR"
    if not observations["total_before_filter"]:
        return "TR_RESPONSE_WITHOUT_REAL_CALLBACKS" if initial_tr["response_count"] else "NO_REAL_CALLBACKS_OBSERVED"
    if not sum(accepted.values()):
        return "REAL_CALLBACKS_FILTERED_OR_INVALID"
    if not all(accepted.values()):
        return "PARTIAL_REQUIRED_REAL_TYPES"
    return "REQUIRED_REAL_TYPES_OBSERVED_NOT_YET_FIELD_VERIFIED"
