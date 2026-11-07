"""Provider-specific requests and list metadata, without article/body extraction."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decode, digest, encode

NAVER_URL = "https://openapi.naver.com/v1/search/news.json"
KRX_URL = "https://open.krx.co.kr/main/main.jsp"
SOURCES = ("NAVER_NEWS", "KIND_RSS", "KRX_PAGE")
COMMON = {"schema_version", "source", "max_pages", "max_requests", "max_attempts",
          "min_interval_seconds", "timeout_seconds", "max_response_bytes"}
DEFAULTS = {"schema_version": 1, "max_pages": 2, "max_requests": 4, "max_attempts": 2,
            "min_interval_seconds": 1, "timeout_seconds": 15, "max_response_bytes": 4 * 1024 * 1024}


def valid_url(value, hosts=None, *, reference=False):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ValidationError("A bounded HTTPS URL is required")
    try:
        p = urlsplit(value)
        schemes = ("http", "https") if reference else ("https",)
        port = 80 if p.scheme == "http" else 443
        if p.scheme not in schemes or not p.hostname or p.username or p.password or p.port not in (None, port) or (p.fragment and not reference):
            raise ValueError
    except ValueError as exc:
        raise ValidationError("Invalid HTTPS URL") from exc
    if hosts is not None and p.hostname not in hosts:
        raise ValidationError("Source URL host is not allowed")
    return value


def config(value):
    if not isinstance(value, dict) or value.get("source") not in SOURCES:
        raise ValidationError("Only NAVER_NEWS, KIND_RSS and KRX_PAGE are in Issue 10")
    source = value["source"]
    specific = {"NAVER_NEWS": {"query", "display"}, "KIND_RSS": {"url"}, "KRX_PAGE": {"url"}}[source]
    if set(value) - COMMON - specific:
        raise ValidationError("Unknown config field; credentials belong only in runtime environment")
    c = DEFAULTS | value
    for key, lo, hi in (("schema_version", 1, 1), ("max_pages", 1, 10), ("max_requests", 1, 30),
                        ("max_attempts", 1, 3), ("min_interval_seconds", 1, 60),
                        ("timeout_seconds", 1, 30), ("max_response_bytes", 1, 16 * 1024 * 1024)):
        if type(c[key]) is not int or not lo <= c[key] <= hi:
            raise ValidationError("Source collection limit is invalid")
    if source == "NAVER_NEWS":
        if not isinstance(c.get("query"), str) or not c["query"].strip() or len(c["query"]) > 256:
            raise ValidationError("A bounded Naver search query is required")
        c.setdefault("display", 100)
        if type(c["display"]) is not int or not 1 <= c["display"] <= 100:
            raise ValidationError("Naver display must be between 1 and 100")
    elif source == "KIND_RSS":
        valid_url(c.get("url"), {"kind.krx.co.kr"})
        p = urlsplit(c["url"])
        # Copy the RSS URL from KIND's official UI. No fabricated API or credentials.
        if not p.path.startswith("/disclosure/"):
            raise ValidationError("Use the disclosure RSS URL copied from KIND")
        allowed = {"method", "repIsuSrtCd", "mktTpCd", "searchCorpName", "currentPageSize"}
        pairs = parse_qsl(p.query, keep_blank_values=True)
        if any(k not in allowed for k, _ in pairs) or len({k for k, _ in pairs}) != len(pairs):
            raise ValidationError("KIND RSS URL contains unsupported query fields")
    else:
        c.setdefault("url", KRX_URL)
        valid_url(c["url"], {"open.krx.co.kr", "www.krx.co.kr", "info.krx.co.kr"})
        p = urlsplit(c["url"])
        if p.query or not p.path.startswith(("/main/", "/contents/")):
            raise ValidationError("KRX page collection requires a public page URL without query credentials")
    return c


@dataclass(frozen=True)
class Request:
    source: str
    url: str
    cursor: int

    def public(self):
        return {"source": self.source, "url": self.url, "cursor": self.cursor, "method": "GET"}


def request(c, cursor=1):
    if c["source"] == "NAVER_NEWS":
        if type(cursor) is not int or not 1 <= cursor <= 1000:
            raise ValidationError("Naver search cursor is outside the supported window")
        params = {"query": c["query"], "display": min(c["display"], 1001 - cursor), "start": cursor, "sort": "date"}
        url = NAVER_URL + "?" + urlencode(params)
    else:
        if cursor != 1:
            raise ValidationError("This source has no verified page cursor")
        url = c["url"]
    return Request(c["source"], url, cursor)


def publication(value, meaning):
    result = {"raw": value, "at": None, "date": None, "precision": "UNKNOWN", "meaning": meaning}
    if not isinstance(value, str) or not value.strip():
        return result
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            result.update(date=date.fromisoformat(value).isoformat(), precision="DATE")
        except ValueError:
            pass
        return result
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is not None and at.utcoffset() is not None:
            result.update(at=at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"), precision="SECOND")
    except (ValueError, TypeError, OverflowError):
        pass
    return result


def document(key, title, snippet, url, published, kind, metadata):
    if not isinstance(key, str) or not key or not isinstance(title, str) or (snippet is not None and not isinstance(snippet, str)):
        raise ValidationError("Source item identity/title/snippet is invalid")
    if url is not None:
        valid_url(url, reference=True)
    return {"item_key": key, "title": title, "snippet": snippet, "url": url,
            "publication": published, "content_kind": kind, "metadata": metadata}


def parse(c, req, raw, media_type):
    """Deterministic metadata normalization. Returned hashes exclude collection clocks."""
    source = c["source"]
    if source == "NAVER_NEWS":
        body = decode(raw)
        if not isinstance(body, dict) or "errorCode" in body:
            raise ValidationError("Naver API error or invalid response")
        total, start, display, items = (body.get(k) for k in ("total", "start", "display", "items"))
        requested = min(c["display"], 1001 - req.cursor)
        if (type(total) is not int or total < 0 or type(start) is not int or start != req.cursor
                or type(display) is not int or not 0 <= display <= requested
                or not isinstance(items, list) or len(items) > display or len(items) > total):
            raise ValidationError("Naver pagination response is inconsistent")
        docs = []
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("Invalid Naver item")
            link = item.get("link") or item.get("originallink")
            valid_url(link, reference=True)
            original = item.get("originallink")
            if original:
                valid_url(original, reference=True)
            docs.append(document(link, item.get("title"), item.get("description"), link,
                                 publication(item.get("pubDate"), "PROVIDED_TO_NAVER_OR_ORIGINAL"),
                                 "SEARCH_SNIPPET", item))
        end = start + len(items) - 1
        if not items:
            stop = "EMPTY_PAGE" if total == 0 or start > total else "INCONSISTENT_SHORT_PAGE"
        elif end >= total:
            stop = "REPORTED_TOTAL_REACHED"
        elif len(items) < requested:
            stop = "INCONSISTENT_SHORT_PAGE"
        elif start + requested > 1000:
            stop = "SEARCH_WINDOW_LIMIT"
        else:
            stop = None
        return {"documents": docs, "next_cursor": None if stop else start + requested,
                "coverage": {"kind": "QUERY_WINDOW", "reported_total": total, "start": start,
                             "returned": len(items), "stop_reason": stop,
                             "dataset_complete": False, "snapshot_consistent": False}}
    if source == "KIND_RSS":
        # Limit encodings and reject DTD/entities before handing input to Expat.
        match = re.search(br'encoding=[\'"]([A-Za-z0-9_-]+)[\'"]', raw[:200], re.I)
        charset = match.group(1).decode("ascii").lower() if match else "utf-8-sig"
        if charset not in ("utf-8", "utf-8-sig", "euc-kr", "cp949"):
            raise ValidationError("Unsupported RSS encoding")
        try:
            text = raw.decode(charset)
            if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
                raise ValidationError("RSS DTD/entity declarations are not supported")
            root = ET.fromstring(text)
        except (UnicodeError, ET.ParseError) as exc:
            raise ValidationError("Invalid RSS response") from exc
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is None:
            raise ValidationError("Expected RSS channel, not an HTML login/error page")
        items = channel.findall("item")
        if len(items) > 1000:
            raise ValidationError("RSS item count exceeds collection bound")
        docs = []
        for item in items:
            fields = {k: item.findtext(k) for k in ("guid", "link", "title", "description", "pubDate")}
            key = fields["guid"] or fields["link"]
            if not key:
                raise ValidationError("RSS item lacks guid/link identity")
            # Identity is feed-scoped. Cross-feed/event dedup belongs to Issue 12.
            key = digest(encode([req.url, key]))
            docs.append(document(key, fields["title"], fields["description"], fields["link"],
                                 publication(fields["pubDate"], "RSS_PUBDATE_UNVERIFIED"), "DISCLOSURE_METADATA", fields))
        return {"documents": docs, "next_cursor": None,
                "coverage": {"kind": "RSS_SNAPSHOT", "returned": len(items), "stop_reason": "RSS_WINDOW_UNKNOWN",
                             "dataset_complete": False, "snapshot_consistent": False}}
    if "html" not in media_type.lower() or not raw.strip():
        raise ValidationError("KRX page capture requires a nonempty HTML entity")
    return {"documents": [document(req.url, "", None, req.url, publication(None, "NOT_PROVIDED"),
                                   "HTML_PAGE_SNAPSHOT", {"body_sha256": digest(raw), "content_verified": False})],
            "next_cursor": None, "coverage": {"kind": "RAW_PAGE_ONLY", "returned": 1,
                "stop_reason": "ARTICLE_EXTRACTION_PENDING_ISSUE_11", "dataset_complete": False,
                "snapshot_consistent": False}}
