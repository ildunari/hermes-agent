from __future__ import annotations

from dataclasses import replace
import json
import logging
from pathlib import Path

import pytest

from gateway.contact_memory.link_research import (
    FetchedPage,
    LinkResearchRequest,
    LinkResearchResult,
    MetadataSearchResearchProvider,
    PinnedHttpsTransport,
    ResearchSource,
    SafeFetchPolicy,
    extract_public_metadata,
)


class _FakeConnection:
    def __init__(self, *, peer: str, response: FetchedPage):
        self.peer = peer
        self.response = response

    def request(self, host: str, target: str, *, headers: dict[str, str]) -> FetchedPage:
        assert "Cookie" not in headers and "Authorization" not in headers
        return replace(self.response, peer_ip=self.peer)

    def close(self) -> None:
        pass


def test_policy_rejects_non_https_credentials_tokens_actions_and_private_dns():
    policy = SafeFetchPolicy()
    rejected = {
        "http://example.com/": "blocked_scheme",
        "https://user:pass@example.com/": "blocked_credentials",
        "https://example.com/unsubscribe?id=1": "blocked_action",
        "https://example.com/%75nsubscribe?id=1": "blocked_action",
        "https://example.com/%72eset?verificationToken=abc": "blocked_action",
        "https://example.com/%2575nsubscribe?id=1": "blocked_action",
        "https://example.com/?access_token=abc": "blocked_token",
        "https://example.com/path?verificationToken=abc": "blocked_token",
        "https://example.com/path?verification%2554oken=abc": "blocked_token",
        "https://example.com/a/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG": "blocked_high_entropy",
    }
    for url, code in rejected.items():
        assert policy.admit(url).code == code
    assert policy.validate_addresses(("93.184.216.34",)).allowed
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "ff02::1"):
        assert policy.validate_addresses((address,)).code == "blocked_address"
    assert policy.validate_addresses(("93.184.216.34", "127.0.0.1")).code == "blocked_address"


def test_transport_pins_dns_peer_and_revalidates_redirects():
    pages = {
        ("example.com", "93.184.216.34"): FetchedPage(
            status=302, content_type="text/html", body=b"", headers={"location": "https://public.example/final"}
        ),
        ("public.example", "93.184.216.35"): FetchedPage(
            status=200, content_type="text/html", body=b"<title>Public title</title>", headers={}
        ),
    }
    resolutions = {"example.com": ("93.184.216.34",), "public.example": ("93.184.216.35",)}
    transport = PinnedHttpsTransport(
        resolver=lambda host: resolutions[host],
        connector=lambda host, address, timeout: _FakeConnection(peer=address, response=pages[(host, address)]),
    )
    page = transport.fetch("https://example.com/start")
    assert page.status == 200 and page.body == b"<title>Public title</title>"

    rebound = PinnedHttpsTransport(
        resolver=lambda _host: ("93.184.216.34",),
        connector=lambda host, address, timeout: _FakeConnection(
            peer="127.0.0.1",
            response=FetchedPage(status=200, content_type="text/html", body=b"no", headers={}),
        ),
    )
    with pytest.raises(RuntimeError, match="blocked_peer"):
        rebound.fetch("https://example.com/")


def test_transport_percent_encodes_unicode_request_target():
    targets: list[str] = []

    class _CaptureConnection(_FakeConnection):
        def request(self, host: str, target: str, *, headers: dict[str, str]) -> FetchedPage:
            targets.append(target)
            target.encode("ascii")
            return super().request(host, target, headers=headers)

    page = FetchedPage(status=200, content_type="text/html", body=b"ok", headers={})
    transport = PinnedHttpsTransport(
        resolver=lambda _host: ("93.184.216.34",),
        connector=lambda host, address, timeout: _CaptureConnection(peer=address, response=page),
    )
    assert transport.fetch("https://example.com/песма?q=žurka").body == b"ok"
    assert targets == ["/%D0%BF%D0%B5%D1%81%D0%BC%D0%B0?q=%C5%BEurka"]


def test_redirect_to_private_or_http_is_rejected():
    for location, code in (("https://127.0.0.1/private", "blocked_address"), ("http://example.com/", "blocked_scheme")):
        transport = PinnedHttpsTransport(
            resolver=lambda _host: ("93.184.216.34",),
            connector=lambda host, address, timeout: _FakeConnection(
                peer=address,
                response=FetchedPage(status=302, content_type="text/html", body=b"", headers={"location": location}),
            ),
        )
        with pytest.raises(RuntimeError, match=code):
            transport.fetch("https://example.com/")


def test_metadata_is_sanitized_and_bounded():
    page = FetchedPage(
        status=200,
        content_type="text/html; charset=utf-8",
        body=(
            b"<html><head><title>  A <b>title</b> </title>"
            b"<meta property='og:description' content='Useful\x00 description'>"
            b"<script type='application/ld+json'>{\"@type\":\"MusicEvent\",\"name\":\"Public Show\"}</script>"
            b"</head><body>Body words about music and concerts.</body></html>"
        ),
        headers={},
    )
    metadata = extract_public_metadata(page)
    dumped = json.dumps(metadata)
    assert "Useful description" in dumped and "Public Show" in dumped
    assert "\u0000" not in dumped and len(metadata["body_summary"]) <= 1000


class _Fetch:
    def fetch(self, _url: str) -> FetchedPage:
        return FetchedPage(
            status=200,
            content_type="text/html",
            body=b"<title>Lady Gaga concert</title><meta property='og:description' content='Music festival'>",
            headers={},
        )


class _Search:
    name = "fake-search"

    def search_public(self, terms: tuple[str, ...], *, limit: int):
        assert all("https://" not in term for term in terms)
        assert not {"private-looking-id", "unpublished", "project-codename"} & set(terms)
        return (
            ResearchSource(
                support_id="a" * 64,
                source_quality="primary",
                recency_band="current",
                entity_type="music_artist",
                canonical_label="Lady Gaga",
                ontology_ids=("music.general",),
            ),
        )


def test_provider_corroborates_without_sending_url_or_fabricating(caplog: pytest.LogCaptureFixture):
    provider = MetadataSearchResearchProvider(fetcher=_Fetch(), search_provider=_Search())
    request = LinkResearchRequest(
        evidence_id="b" * 64,
        url="https://opaque.example/private-looking-id",
        platform="public-page",
        shared_by="stephen-lucier",
        occurred_at=1_700_000_000,
        positive_reactors=("kosta-owner",),
    )
    with caplog.at_level(logging.DEBUG):
        result = provider.research(request)
    assert result.status == "ok"
    assert result.sources and "music.general" in result.ontology_ids
    assert result.public_entities == (("music_artist", "Lady Gaga"),)
    assert request.url not in caplog.text

    class _Failure:
        def fetch(self, _url: str) -> FetchedPage:
            raise RuntimeError("timeout")

    failed = MetadataSearchResearchProvider(fetcher=_Failure()).research(request)
    assert failed == LinkResearchResult.failure(request.evidence_id, "fetch_timeout")


def test_arbitrary_private_metadata_is_never_exported_to_public_search():
    class _PrivateFetch:
        def fetch(self, url: str) -> FetchedPage:
            del url
            return FetchedPage(
                status=200, content_type="text/html",
                body=b"<title>Unpublished Project-Codename Person-12345</title>", headers={},
            )

    class _CaptureSearch:
        name = "capture"

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def search_public(self, terms: tuple[str, ...], *, limit: int):
            self.calls.append(terms)
            return ()

    search = _CaptureSearch()
    result = MetadataSearchResearchProvider(
        fetcher=_PrivateFetch(), search_provider=search,
    ).research(LinkResearchRequest(
        evidence_id="c" * 64, url="https://example.com/private", platform="web",
        shared_by="stephen-lucier", occurred_at=1.0,
    ))
    assert result.status == "ok"
    assert search.calls == []
