"""Regression tests for app/utils/downloader.py SSRF / size fixes.

requests is mocked throughout; nothing touches the network. Hostnames used
are either IP literals or names that fail to resolve offline, which the
private-address check treats as "not private" — the same assumption the
existing TestSSRFValidation tests make.
"""

import os
from unittest.mock import MagicMock

import pytest
import requests

from app.utils import downloader as dl


@pytest.fixture(autouse=True)
def reset_ssrf():
    yield
    dl.configure_ssrf(allowed_domains=None, require_https=True)


def _resp(status=200, headers=None, chunks=(), peer=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.iter_content.return_value = iter(chunks)
    if peer is not None:
        r.raw._connection.sock.getpeername.return_value = (peer, 443)
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    return r


def _downloader(tmp_path, responses):
    d = dl.RDSDownloader(data_dir=str(tmp_path))
    d.session.trust_env = False  # ignore any proxy env on the dev machine
    d.session.get = MagicMock(side_effect=list(responses))
    return d


class TestAllowList:
    @pytest.mark.parametrize("host,ok", [
        ("example.org", True),
        ("cdn.example.org", True),
        ("EXAMPLE.ORG", True),
        ("example.org.attacker.net", False),
        ("notexample.org", False),
        ("example.org-evil.com", False),
    ])
    def test_exact_or_subdomain_only(self, host, ok):
        assert dl._host_allowed(host, ["example.org"]) is ok

    def test_validate_remote_url_uses_suffix_match(self):
        dl.configure_ssrf(allowed_domains=["example.org"])
        with pytest.raises(ValueError, match="allow-list"):
            dl.validate_remote_url("https://example.org.attacker.invalid/f.RDS")


class TestRedirects:
    def test_redirect_to_private_address_is_blocked_and_nothing_saved(self, tmp_path):
        d = _downloader(tmp_path, [
            _resp(302, {"Location": "https://127.0.0.1/secret"}),
        ])
        with pytest.raises(ValueError, match="private|internal"):
            d.download("https://files.example.invalid/data.RDS")
        assert os.listdir(tmp_path) == []
        # The internal target was never requested.
        assert d.session.get.call_count == 1

    def test_redirect_body_is_never_saved_as_the_file(self, tmp_path):
        d = _downloader(tmp_path, [
            _resp(302, {"Location": "/real/data.RDS"}, chunks=[b"<html>moved</html>"]),
            _resp(200, {"content-length": "4"}, chunks=[b"RDSX"]),
        ])
        path = d.download("https://files.example.invalid/data.RDS")
        with open(path, "rb") as f:
            assert f.read() == b"RDSX"
        # Relative Location resolved against the current URL.
        assert d.session.get.call_args_list[1].args[0] == "https://files.example.invalid/real/data.RDS"

    def test_too_many_redirects(self, tmp_path):
        hops = [_resp(302, {"Location": f"/hop{i}"}) for i in range(dl.MAX_REDIRECTS + 1)]
        d = _downloader(tmp_path, hops)
        with pytest.raises(requests.TooManyRedirects):
            d.download("https://files.example.invalid/data.RDS")
        assert os.listdir(tmp_path) == []

    def test_redirect_without_location_is_an_error(self, tmp_path):
        d = _downloader(tmp_path, [_resp(301, {})])
        with pytest.raises(requests.HTTPError):
            d.download("https://files.example.invalid/data.RDS")

    def test_cross_host_redirect_drops_credentials(self, tmp_path, monkeypatch):
        """Neither the bearer token nor the (non-host-scoped) cookies follow."""
        d = _downloader(tmp_path, [_resp(302, {"Location": "https://other.example.invalid/x.RDS"})])
        d.set_bearer_token("tok")
        d.set_cookies({"session": "secret"})

        sent = {}

        def other_host_get(self, url, **kwargs):
            sent['headers'] = dict(self.headers)
            sent['cookies'] = self.cookies.get_dict()
            return _resp(200, chunks=[b"ok"])

        monkeypatch.setattr(requests.Session, 'get', other_host_get)
        d.download("https://files.example.invalid/data.RDS")
        assert 'Authorization' not in sent['headers']
        assert sent['cookies'] == {}

    def test_error_status_raises_and_saves_nothing(self, tmp_path):
        d = _downloader(tmp_path, [_resp(404)])
        with pytest.raises(requests.HTTPError):
            d.download("https://files.example.invalid/data.RDS")
        assert os.listdir(tmp_path) == []


class TestSizeCap:
    def test_streaming_breach_deletes_partial_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMOTE_RDS_MAX_BYTES", "10")
        d = _downloader(tmp_path, [_resp(200, chunks=[b"x" * 8, b"x" * 8])])
        with pytest.raises(ValueError, match="REMOTE_RDS_MAX_BYTES"):
            d.download("https://files.example.invalid/data.RDS")
        assert os.listdir(tmp_path) == []

    def test_declared_length_over_cap_is_refused_up_front(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMOTE_RDS_MAX_BYTES", "10")
        r = _resp(200, {"content-length": "1000"}, chunks=[b"x"])
        d = _downloader(tmp_path, [r])
        with pytest.raises(ValueError, match="REMOTE_RDS_MAX_BYTES"):
            d.download("https://files.example.invalid/data.RDS")
        r.iter_content.assert_not_called()

    def test_default_cap_is_one_gib(self, monkeypatch):
        monkeypatch.delenv("REMOTE_RDS_MAX_BYTES", raising=False)
        assert dl._max_download_bytes() == 1024 ** 3


class TestDnsRebinding:
    def test_private_peer_is_rejected(self, tmp_path):
        d = _downloader(tmp_path, [_resp(200, chunks=[b"x"], peer="127.0.0.1")])
        with pytest.raises(ValueError, match="DNS rebinding"):
            d.download("https://files.example.invalid/data.RDS")
        assert os.listdir(tmp_path) == []

    def test_public_peer_is_accepted(self, tmp_path):
        d = _downloader(tmp_path, [_resp(200, chunks=[b"x"], peer="93.184.216.34")])
        assert os.path.exists(d.download("https://files.example.invalid/data.RDS"))
