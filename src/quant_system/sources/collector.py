"""Explicit bounded collection, independent of the market/order loop."""

import time

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import digest, encode, now_utc

from .adapters import config, parse, request
from .journal import CollectionJournal
from .transport import retry_delay

RETRYABLE = {408, 429, 500, 502, 503, 504}


def collect(store, run_id, settings, transport, *, sleep=time.sleep):
    c = config(settings)
    if store.connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        raise ValidationError("Use a fresh run ID for each collection session")
    synthetic = transport.synthetic
    origin = "SYNTHETIC" if synthetic else "OBSERVED"
    store.register_run(run_id, mode="SYNTHETIC_REPLAY" if synthetic else "SHADOW", config=c,
                       data_origin=origin, code_version="source-collection-v1")
    journal = CollectionJournal(store, run_id)
    cursor, attempts, pages, documents = 1, 0, 0, 0
    repeated, changed = 0, 0
    stop, last_error = None, None
    refs, coverages = [], []
    while pages < c["max_pages"] and attempts < c["max_requests"]:
        req = request(c, cursor)
        parsed = None
        for attempt in range(c["max_attempts"]):
            if attempts >= c["max_requests"]:
                stop = "REQUEST_BUDGET_REACHED"
                break
            response = transport.fetch(req, c)
            attempts += 1
            capture_id = journal.raw(req, response, attempts)
            error = response.error
            if not error and response.status != 200:
                error = "HTTP_STATUS" if response.status is not None else "TRANSPORT_ERROR"
            parsed = None
            if error is None:
                try:
                    parsed = parse(c, req, response.body, response.media_type)
                except (ValidationError, ValueError, TypeError, KeyError):
                    error = "INVALID_SOURCE_RESPONSE"
            receipt = journal.receipt(c, req, response, capture_id, parsed, error)
            refs.append(capture_id.replace(":raw:", ":fetch:"))
            last_error = error
            if parsed is not None:
                statuses = receipt["observation_statuses"]
                documents += len(statuses)
                repeated += statuses.count("REPEATED_VARIANT")
                changed += statuses.count("NEW_VARIANT")
                coverages.append(parsed["coverage"])
                break
            retryable = response.error == "TRANSPORT_ERROR" or (response.error is None and response.status in RETRYABLE)
            if not retryable:
                stop = "SOURCE_ERROR"
                break
            delay = retry_delay(response.retry_after, c["min_interval_seconds"])
            if delay is None:
                stop = "RETRY_AFTER_EXCEEDS_SESSION_BOUND"
                break
            if attempt + 1 == c["max_attempts"]:
                stop = "RETRY_BUDGET_REACHED"
                break
            if attempts < c["max_requests"] and not synthetic:
                sleep(delay)
        if parsed is None:
            stop = stop or "REQUEST_BUDGET_REACHED"
            break
        pages += 1
        cursor = parsed["next_cursor"]
        if cursor is None:
            stop = parsed["coverage"]["stop_reason"]
            break
        if pages < c["max_pages"] and attempts < c["max_requests"] and not synthetic:
            sleep(c["min_interval_seconds"])
    stop = stop or ("REQUEST_BUDGET_REACHED" if attempts >= c["max_requests"] else "PAGE_BUDGET_REACHED")
    raw_only = c["source"] == "KRX_PAGE" and pages > 0
    result = {"schema": "source-collection-report-v1", "run_id": run_id, "source": c["source"],
              "data_origin": origin, "config_sha256": digest(encode(c)), "requests": attempts, "pages": pages,
              "document_observations": documents, "repeated_variants": repeated, "new_variants": changed,
              "stop_reason": stop, "error": last_error, "fetch_refs": refs, "coverage": coverages,
              "raw_only": raw_only, "dataset_complete": False, "live_access_verified": False,
              "http_success_observed": not synthetic and pages > 0,
              "metadata_normalized": pages > 0 and not raw_only,
              "collection_ok": pages > 0 and last_error is None and stop != "INCONSISTENT_SHORT_PAGE"}
    store.put_artifact(run_id + ":source:report", run_id, result, ready_at=now_utc())
    return result
