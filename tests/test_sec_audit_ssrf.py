import socket

import pytest

from server.tool_registry import _assert_public_fetch_url


def test_fetch_url_denies_loopback_private_and_metadata_targets():
    for url in (
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "file:///etc/passwd",
        "http://user:password@example.com/",
    ):
        with pytest.raises(ValueError):
            _assert_public_fetch_url(url)


def test_fetch_url_rejects_hostname_if_any_dns_answer_is_non_public(monkeypatch):
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [public, private])
    with pytest.raises(ValueError):
        _assert_public_fetch_url("https://mixed-answer.example/")


def test_fetch_url_accepts_public_hostname_only_when_resolved_publicly(monkeypatch):
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [public])
    assert _assert_public_fetch_url("https://example.com/docs") == "https://example.com/docs"
