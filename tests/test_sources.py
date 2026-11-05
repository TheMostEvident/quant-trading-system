import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, encode
from quant_system.sources.adapters import NAVER_URL, Request, config, parse, publication, request
from quant_system.sources.cli import run_collection
from quant_system.sources.collector import collect
from quant_system.sources.journal import CollectionJournal, verify_collection
from quant_system.sources.transport import FixtureTransport, HTTPTransport, Response, retry_delay

HERE = Path(__file__).parent / "fixtures" / "sources"
AT = "2026-11-04T01:00:00.000000Z"


class SequenceTransport:
    synthetic = True

    def __init__(self, rows, *, observed=False):
        self.rows = iter(rows)
        self.calls = 0
        self.synthetic = not observed

    def fetch(self, req, c):
        self.calls += 1
        status, body, retry, error = next(self.rows)
        return Response(status, body, AT, self.calls, "application/json", retry, error)


class Entity(io.BytesIO):
    code = 200

    def __init__(self, raw, *, code=200, headers=None):
        super().__init__(raw)
        self.code = code
        self.headers = headers or {"Content-Type": "application/json"}


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SourceAdapters(unittest.TestCase):
    def setUp(self):
        self.c = config(decode((HERE / "naver-config.json").read_bytes()))
        self.raw = (HERE / "naver-page1.json").read_bytes()

    def test_scope_rejects_other_sources(self):
        for source in ("DART", "SEC", "GENERIC", "KRX_API"):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                config({"source": source})

    def test_config_rejects_secrets_unknown_fields_and_invalid_limits(self):
        for change in ({"secret": "private"}, {"max_pages": 0}, {"max_requests": True},
                       {"max_attempts": 4}, {"min_interval_seconds": 0}, {"display": 101}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                config(self.c | change)

    def test_urls_do_not_accept_credentials_other_hosts_or_secrets(self):
        for url in ("http://kind.krx.co.kr/disclosure/rss", "https://kind.krx.co.kr.evil.invalid/disclosure/rss",
                    "https://user:pass@kind.krx.co.kr/disclosure/rss", "https://kind.krx.co.kr:444/disclosure/rss",
                    "https://kind.krx.co.kr/disclosure/rss?token=private", "https://kind.krx.co.kr/disclosure/rss#fragment"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                config({"source": "KIND_RSS", "url": url})

    def test_krx_does_not_use_statistics_or_arbitrary_api_as_event_feed(self):
        for url in ("https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd", "https://open.krx.co.kr/main/main.jsp?AUTH_KEY=private"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                config({"source": "KRX_PAGE", "url": url})

    def test_naver_request_and_provided_time_are_not_article_body(self):
        r = request(self.c)
        self.assertEqual(r.url, NAVER_URL + "?query=synthetic&display=2&start=1&sort=date")
        result = parse(self.c, r, self.raw, "application/json")
        first = result["documents"][0]
        self.assertEqual(first["publication"]["at"], "2026-11-04T00:00:00.000000Z")
        self.assertEqual(first["publication"]["meaning"], "PROVIDED_TO_NAVER_OR_ORIGINAL")
        self.assertEqual(first["content_kind"], "SEARCH_SNIPPET")
        self.assertEqual(first["snippet"], "합성 <b>요약</b> A")
        self.assertFalse(result["coverage"]["dataset_complete"])
        self.assertEqual(result["next_cursor"], 3)

    def test_date_and_naive_or_bad_time_never_become_utc_midnight(self):
        day = publication("2026-11-04", "test")
        self.assertEqual(day["precision"], "DATE")
        self.assertIsNone(day["at"])
        for value in (None, "bad", "2026-02-30", "Wed, 04 Nov 2026 10:00:00"):
            with self.subTest(value=value):
                self.assertEqual(publication(value, "test")["precision"], "UNKNOWN")
                self.assertIsNone(publication(value, "test")["at"])

    def test_http_original_article_is_a_reference_and_is_not_fetched(self):
        body = decode(self.raw)
        body["items"][0]["originallink"] = "http://publisher.invalid/a#section"
        parsed = parse(self.c, request(self.c), encode(body), "application/json")
        self.assertEqual(parsed["documents"][0]["metadata"]["originallink"], "http://publisher.invalid/a#section")

    def test_short_search_page_does_not_claim_exhaustion(self):
        body = decode(self.raw)
        body["items"] = body["items"][:1]
        parsed = parse(self.c, request(self.c), encode(body), "application/json")
        self.assertEqual(parsed["coverage"]["stop_reason"], "INCONSISTENT_SHORT_PAGE")
        self.assertIsNone(parsed["next_cursor"])

    def test_search_window_stops_without_requesting_1001(self):
        body = decode(self.raw) | {"start": 999, "total": 5000}
        result = parse(self.c, request(self.c, 999), encode(body), "application/json")
        self.assertEqual(result["coverage"]["stop_reason"], "SEARCH_WINDOW_LIMIT")
        self.assertIsNone(result["next_cursor"])
        with self.assertRaises(ValidationError):
            request(self.c, 1001)

    def test_api_error_captcha_duplicate_json_keys_and_bad_cursor_rejected(self):
        for body in (b'{"errorCode":"024"}', b'<html>login</html>', b'{"total":0,"total":1}',
                     encode(decode(self.raw) | {"start": 3}), encode(decode(self.raw) | {"total": 0})):
            with self.subTest(body=body[:40]), self.assertRaises(ValidationError):
                parse(self.c, request(self.c), body, "application/json")

    def test_kind_feed_identity_and_unknown_window(self):
        c = config(decode((HERE / "kind-config.json").read_bytes()))
        result = parse(c, request(c), (HERE / "kind-page.xml").read_bytes(), "application/rss+xml")
        self.assertEqual(len(result["documents"]), 2)
        self.assertEqual(result["documents"][0]["content_kind"], "DISCLOSURE_METADATA")
        self.assertEqual(result["documents"][1]["publication"]["precision"], "DATE")
        self.assertEqual(result["coverage"]["stop_reason"], "RSS_WINDOW_UNKNOWN")
        self.assertFalse(result["coverage"]["dataset_complete"])

    def test_rss_html_entity_expansion_and_utf16_nul_bypass_rejected(self):
        c = config(decode((HERE / "kind-config.json").read_bytes()))
        xml = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><title>&x;</title></channel></rss>'
        for body in (b'<html>Login</html>', xml.encode(), xml.encode("utf-16le"), b'<rss><channel><item><title>No ID</title></item></channel></rss>'):
            with self.subTest(body=body[:20]), self.assertRaises(ValidationError):
                parse(c, request(c), body, "text/xml")

    def test_krx_is_only_a_page_snapshot_with_unknown_publication(self):
        c = config({"source": "KRX_PAGE"})
        parsed = parse(c, request(c), b'<html>challenge or content</html>', "text/html")
        doc = parsed["documents"][0]
        self.assertEqual(doc["content_kind"], "HTML_PAGE_SNAPSHOT")
        self.assertFalse(doc["metadata"]["content_verified"])
        self.assertIsNone(doc["publication"]["at"])
        self.assertEqual(parsed["coverage"]["kind"], "RAW_PAGE_ONLY")
        with self.assertRaises(ValidationError):
            parse(c, request(c), b'{}', "application/json")


class SourceCollection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JournalStore(Path(self.temp.name) / "sources.sqlite")
        self.addCleanup(self.store.close)
        self.c = config(decode((HERE / "naver-config.json").read_bytes()))
        self.body = (HERE / "naver-page1.json").read_bytes()

    def docs(self):
        return [decode(r[0]) for r in self.store.connection.execute("SELECT a.data FROM collection_items i JOIN artifacts a USING(artifact_id) JOIN refs r ON r.ref_id=a.artifact_id ORDER BY r.seq")]

    def test_raw_first_lineage_duplicates_and_changed_content(self):
        result = collect(self.store, "run1", self.c, FixtureTransport(HERE / "naver-responses.json"))
        self.assertEqual((result["requests"], result["document_observations"], result["repeated_variants"], result["new_variants"]), (2, 4, 1, 1))
        self.assertTrue(result["collection_ok"])
        self.assertFalse(result["live_access_verified"])
        self.assertFalse(result["http_success_observed"])
        docs = self.docs()
        self.assertEqual(docs[2]["first_received_at"], AT)
        self.assertEqual(docs[2]["same_variant_ref"], docs[2]["first_observation_ref"])
        self.assertEqual(self.store.raw(docs[0]["capture_ref"]), self.body)
        self.assertGreater(docs[0]["ready_at"], docs[0]["received_at"])
        self.assertEqual(verify_collection(self.store, "run1")["reparsed_fetches"], 2)

    def test_identity_history_survives_new_run_without_duplicating_semantic_events(self):
        collect(self.store, "run1", self.c, FixtureTransport(HERE / "naver-responses.json"))
        result = collect(self.store, "run2", self.c, FixtureTransport(HERE / "naver-responses.json"))
        self.assertEqual(result["repeated_variants"], 4)
        self.assertEqual(result["new_variants"], 0)
        self.assertEqual(len(self.docs()), 8)  # all collection observations remain present
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_synthetic_first_seen_cannot_preempt_observed_identity(self):
        collect(self.store, "synthetic", self.c, FixtureTransport(HERE / "naver-responses.json"))
        transport = SequenceTransport([(200, self.body, None, None)], observed=True)
        result = collect(self.store, "observed", self.c | {"max_pages": 1}, transport, sleep=lambda _: None)
        self.assertEqual(result["repeated_variants"], 0)
        self.assertEqual(self.docs()[-2]["observation_status"], "FIRST_OBSERVATION")
        self.assertEqual(self.docs()[-2]["data_origin"], "OBSERVED")

    def test_failed_projection_commit_retains_raw_and_rolls_back_items(self):
        self.store.register_run("crash", mode="SYNTHETIC_REPLAY", config=self.c)
        journal = CollectionJournal(self.store, "crash")
        req = request(self.c)
        response = Response(200, self.body, AT, 1, "application/json")
        raw_ref = journal.raw(req, response, 1)
        parsed = parse(self.c, req, self.body, response.media_type)
        def crash():
            raise RuntimeError("transaction interrupted")
        with self.assertRaises(RuntimeError):
            journal.receipt(self.c, req, response, raw_ref, parsed, None, before_commit=crash)
        self.assertEqual(self.store.raw(raw_ref), self.body)
        for table in ("collection_fetches", "collection_items", "artifacts"):
            self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        journal.receipt(self.c, req, response, raw_ref, parsed, None)
        self.assertTrue(verify_collection(self.store, "crash")["verified"])

    def test_http_429_retry_is_bounded_and_both_entities_are_retained(self):
        waits = []
        t = SequenceTransport([(429, b'limited', "2", None), (200, self.body, None, None)], observed=True)
        result = collect(self.store, "retry", self.c | {"max_pages": 1}, t, sleep=waits.append)
        self.assertEqual(waits, [2])
        self.assertEqual(result["requests"], 2)
        self.assertTrue(result["collection_ok"])
        self.assertEqual(self.store.raw("retry:source:raw:1"), b'limited')
        self.assertEqual(verify_collection(self.store, "retry")["fetches"], 2)

    def test_long_retry_after_stops_rather_than_retrying_early(self):
        t = SequenceTransport([(429, b'limited', "61", None)])
        result = collect(self.store, "long", self.c, t)
        self.assertEqual(t.calls, 1)
        self.assertEqual(result["stop_reason"], "RETRY_AFTER_EXCEEDS_SESSION_BOUND")
        self.assertFalse(result["collection_ok"])

    def test_404_and_malformed_200_do_not_retry_or_succeed(self):
        for i, status in enumerate((404, 200)):
            t = SequenceTransport([(status, b'<html>error</html>', None, None)])
            result = collect(self.store, "error" + str(i), self.c, t)
            self.assertEqual(t.calls, 1)
            self.assertFalse(result["collection_ok"])
            self.assertEqual(result["document_observations"], 0)

    def test_transport_failure_and_retry_exhaustion_remain_visible(self):
        t = SequenceTransport([(None, b'', None, "TRANSPORT_ERROR")] * 3)
        result = collect(self.store, "failed", self.c, t)
        self.assertEqual(t.calls, 2)
        self.assertEqual(result["stop_reason"], "RETRY_BUDGET_REACHED")
        self.assertEqual(result["error"], "TRANSPORT_ERROR")

    def test_request_and_page_budgets_do_not_claim_full_dataset(self):
        for name, limits, reason in (("page", {"max_pages": 1}, "PAGE_BUDGET_REACHED"),
                                     ("request", {"max_requests": 1}, "REQUEST_BUDGET_REACHED")):
            result = collect(self.store, name, self.c | limits, FixtureTransport(HERE / "naver-responses.json"))
            self.assertEqual(result["stop_reason"], reason)
            self.assertFalse(result["dataset_complete"])

    def test_run_reuse_is_rejected_before_another_request(self):
        collect(self.store, "once", self.c, FixtureTransport(HERE / "naver-responses.json"))
        t = SequenceTransport([])
        with self.assertRaises(ValidationError):
            collect(self.store, "once", self.c, t)
        self.assertEqual(t.calls, 0)

    def test_collection_history_is_immutable(self):
        collect(self.store, "immutable", self.c, FixtureTransport(HERE / "naver-responses.json"))
        for table in ("collection_fetches", "collection_items"):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute("DELETE FROM " + table)

    def test_krx_raw_only_is_never_metadata_or_live_verification(self):
        result = collect(self.store, "krx", {"source": "KRX_PAGE"}, FixtureTransport(HERE / "krx-responses.json"))
        self.assertTrue(result["raw_only"])
        self.assertFalse(result["metadata_normalized"])
        self.assertFalse(result["live_access_verified"])
        self.assertTrue(verify_collection(self.store, "krx")["verified"])

    def test_kind_fixture_replay_and_original_bytes(self):
        c = decode((HERE / "kind-config.json").read_bytes())
        result = collect(self.store, "kind", c, FixtureTransport(HERE / "kind-responses.json"))
        self.assertEqual(result["document_observations"], 2)
        self.assertTrue(verify_collection(self.store, "kind")["verified"])
        self.assertEqual(self.store.raw("kind:source:raw:1"), (HERE / "kind-page.xml").read_bytes())


class SourceTransport(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"NAVER_CLIENT_ID": "synthetic-client", "NAVER_CLIENT_SECRET": "synthetic-private"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.c = config({"source": "NAVER_NEWS", "query": "synthetic"})

    def test_auth_headers_are_runtime_only_and_not_in_public_request_or_repr(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(Entity(b'{}')))
        r = request(self.c)
        result = t.fetch(r, self.c)
        self.assertEqual(result.status, 200)
        self.assertNotIn("synthetic-private", repr(t))
        self.assertNotIn("synthetic-private", str(r.public()))
        req = t.opener.calls[0][0]
        self.assertIn("synthetic-private", req.headers.values())

    def test_missing_auth_fails_before_network(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValidationError):
            HTTPTransport("NAVER_NEWS")

    def test_forged_url_cannot_send_auth_to_another_host(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(Entity(b'{}')))
        with self.assertRaises(ValidationError):
            t.fetch(Request("NAVER_NEWS", "https://other.invalid/", 1), self.c)
        self.assertEqual(t.opener.calls, [])

    def test_reflected_secret_is_not_persisted_as_raw(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(Entity(b'{"error":"synthetic-private"}')))
        result = t.fetch(request(self.c), self.c)
        self.assertEqual(result.body, b'')
        self.assertEqual(result.error, "SENSITIVE_RESPONSE_OMITTED")
        self.assertEqual(result.representation, "OMITTED_SENSITIVE_RESPONSE")

    def test_size_limit_records_only_an_explicit_prefix(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(Entity(b'123456')))
        result = t.fetch(request(self.c), self.c | {"max_response_bytes": 3})
        self.assertEqual(result.body, b'123')
        self.assertEqual(result.error, "RESPONSE_TOO_LARGE")
        self.assertEqual(result.representation, "HTTP_ENTITY_PREFIX")

    def test_http_error_entity_preserved_without_following_redirect(self):
        error = urllib.error.HTTPError(NAVER_URL, 302, "redirect", {"Content-Type": "text/html", "Location": "https://other.invalid/"}, io.BytesIO(b'redirect'))
        t = HTTPTransport("NAVER_NEWS", opener=Opener(error))
        result = t.fetch(request(self.c), self.c)
        self.assertEqual((result.status, result.body), (302, b'redirect'))
        self.assertEqual(len(t.opener.calls), 1)

    def test_network_exception_text_is_never_copied_to_response(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(urllib.error.URLError("synthetic-private")))
        result = t.fetch(request(self.c), self.c)
        self.assertEqual(result.error, "TRANSPORT_ERROR")
        self.assertNotIn("synthetic-private", repr(result))

    def test_unsupported_compression_is_not_parsed_as_an_entity(self):
        t = HTTPTransport("NAVER_NEWS", opener=Opener(Entity(b'compressed', headers={"Content-Encoding": "gzip"})))
        self.assertEqual(t.fetch(request(self.c), self.c).error, "UNSUPPORTED_CONTENT_ENCODING")

    def test_retry_after_has_a_finite_stop(self):
        self.assertEqual(retry_delay("3", 1), 3)
        self.assertIsNone(retry_delay("120", 1))
        self.assertEqual(retry_delay("bad", 2), 2)


class CollectionCLI(unittest.TestCase):
    def test_fixture_cli_reopens_read_only_for_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, report = Path(tmp) / "source.sqlite", Path(tmp) / "report.json"
            result = run_collection(HERE / "naver-config.json", db, "cli", fixture=HERE / "naver-responses.json", report=report)
            self.assertEqual(decode(report.read_bytes()), result)
            with JournalStore(db, read_only=True) as store:
                self.assertTrue(verify_collection(store, "cli")["verified"])
            with self.assertRaises(ValidationError):
                run_collection(HERE / "naver-config.json", db, "other", fixture=HERE / "naver-responses.json", report=report)

    def test_no_default_network_and_no_mixed_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in ({}, {"fixture": HERE / "naver-responses.json", "live": True}):
                with self.subTest(args=args), self.assertRaises(ValidationError):
                    run_collection(HERE / "naver-config.json", Path(tmp) / "source.sqlite", "bad", **args)


if __name__ == "__main__":
    unittest.main()
