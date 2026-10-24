"""
Utility for downloading RDS files from remote sources.
Supports authenticated downloads with session cookies, bearer tokens, and custom headers.
"""

import ipaddress
import os
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, unquote

import requests

from app.utils.logging import get_logger

_logger = get_logger(__name__)

DEFAULT_DATA_DIR = os.path.join("static", "data")


def sanitize_url(url: str) -> str:
    """Strip query string and credentials from a URL for safe logging."""
    parsed = urlparse(url)
    netloc = parsed.hostname or ''
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    cleaned = parsed._replace(query='', netloc=netloc)
    return urlunparse(cleaned)


_SSRF_ALLOWED_DOMAINS: list[str] | None = None
_SSRF_REQUIRE_HTTPS: bool = True


def configure_ssrf(allowed_domains: list[str] | None = None, require_https: bool = True):
    """Configure SSRF protection settings at module level.

    Args:
        allowed_domains: List of allowed domains; a host matches when it equals one or is a
            subdomain of one (``cdn.example.org`` matches ``example.org``). If None, all domains allowed.
        require_https: If True (default), only HTTPS URLs are permitted.
    """
    global _SSRF_ALLOWED_DOMAINS, _SSRF_REQUIRE_HTTPS
    _SSRF_ALLOWED_DOMAINS = allowed_domains
    _SSRF_REQUIRE_HTTPS = require_https


def _is_private_address(host: str) -> bool:
    """Check if a hostname resolves to a private, loopback, or link-local address."""
    try:
        addrs = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    for family, _, _, _, sockaddr in addrs:
        ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
            if addr.is_multicast:
                return True
            # AWS/GCP/Azure metadata endpoints
            if ip.startswith('169.254.'):
                return True
        except ValueError:
            continue
    return False


def _is_metadata_address(host: str) -> bool:
    """Check if host is a known cloud metadata service."""
    metadata_hosts = {
        '169.254.169.254',          # AWS/GCP/Azure
        'metadata.google.internal',  # GCP
        '100.100.100.200',          # Alibaba Cloud
    }
    return host in metadata_hosts or host.lower() in metadata_hosts


def validate_remote_url(url: str) -> str:
    """Validate a URL against SSRF protections.

    Args:
        url: The URL to validate.

    Returns:
        The validated URL.

    Raises:
        ValueError: If the URL fails SSRF validation.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ''

    if not host:
        raise ValueError("URL has no hostname")

    if _SSRF_REQUIRE_HTTPS and parsed.scheme != 'https':
        raise ValueError(f"Only HTTPS URLs are allowed: {sanitize_url(url)}")

    if _is_metadata_address(host):
        raise ValueError(f"Requests to metadata services are blocked: {sanitize_url(url)}")

    if _is_private_address(host):
        raise ValueError(f"Requests to private/internal addresses are blocked: {sanitize_url(url)}")

    if _SSRF_ALLOWED_DOMAINS is not None and not _host_allowed(host, _SSRF_ALLOWED_DOMAINS):
        raise ValueError(f"Domain not in allow-list: {sanitize_url(url)}")

    return url


def _host_allowed(host: str, allowed_domains: list[str]) -> bool:
    """Exact host or a true subdomain of an allowed domain.

    This used to be a substring test (`d in host`), so with `example.org`
    allowed, `example.org.attacker.net` and `notexample.org` both passed.
    """
    host = host.lower().rstrip('.')
    for d in allowed_domains:
        d = d.strip().lower().rstrip('.')
        if d and (host == d or host.endswith('.' + d)):
            return True
    return False


# Redirects are followed by hand so every hop goes through validate_remote_url.
# requests' own following (allow_redirects=True) would fetch an internal
# Location before we could check it.
MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _max_download_bytes() -> int:
    """REMOTE_RDS_MAX_BYTES (default 1 GiB; 0 or less means the default). Read per download."""
    from app.config import get_settings
    value = get_settings().REMOTE_RDS_MAX_BYTES
    return value if value > 0 else 1024 ** 3


def _peer_ip(resp) -> str | None:
    """Best-effort: the IP address the response actually came from.

    DNS rebinding: validate_remote_url resolves the host, then requests
    resolves it AGAIN to connect, and a hostile DNS server can answer public
    the first time and 127.0.0.1 the second. Pinning the resolved IP for the
    connection would need a custom transport adapter (and breaks TLS SNI /
    certificate checks unless done carefully), so instead the connected
    socket's peer address is checked before any body is read. It relies on
    urllib3 internals, so when they are not reachable this returns None and
    the check is skipped rather than failing every download.
    """
    try:
        sock = resp.raw._connection.sock
        ip = sock.getpeername()[0]
        return str(ipaddress.ip_address(ip))
    except Exception:
        return None


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


class RDSDownloader:
    """Downloads RDS files from remote servers with authentication support."""

    def __init__(self, data_dir: Optional[str] = None, logger=None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.session = requests.Session()
        self.logger = logger or _logger

    def set_bearer_token(self, token: str):
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def set_cookies(self, cookies: dict):
        self.session.cookies.update(cookies)

    def set_headers(self, headers: dict):
        self.session.headers.update(headers)

    def set_basic_auth(self, username: str, password: str):
        self.session.auth = (username, password)

    def download(self, url: str, filename: Optional[str] = None) -> str:
        """
        Download an RDS file from a remote URL.

        Args:
            url: Remote URL of the RDS file (may include query params like _xsrf)
            filename: Optional custom filename (defaults to URL path basename)

        Returns:
            Absolute path to the downloaded file

        Raises:
            ValueError: If the URL fails SSRF validation.
        """
        validate_remote_url(url)

        start = time.time()
        parsed = urlparse(url)
        name = filename or os.path.basename(unquote(parsed.path))
        if not name.endswith(".RDS"):
            name += ".RDS"
        dest = os.path.abspath(os.path.join(self.data_dir, name))

        self.logger.info(f"Downloading {sanitize_url(url)}")
        self.logger.info(f"Destination: {dest}")

        max_bytes = _max_download_bytes()
        try:
            resp = self._get_following_redirects(url)
            with resp:
                total = int(resp.headers.get("content-length", 0) or 0)
                # Refuse up front when the server admits the size; the streaming
                # check below still applies, since Content-Length can lie or be
                # absent (chunked encoding).
                if total > max_bytes:
                    raise ValueError(
                        f"Remote file is {total} bytes, over REMOTE_RDS_MAX_BYTES={max_bytes}: {sanitize_url(url)}")
                downloaded = 0

                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError(
                                f"Download exceeded REMOTE_RDS_MAX_BYTES={max_bytes}: {sanitize_url(url)}")
                        f.write(chunk)
                        if total and downloaded % (1024 * 512) == 0:
                            pct = downloaded / total * 100
                            self.logger.debug(f"Downloaded {downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB ({pct:.0f}%)")

            elapsed = time.time() - start
            self.logger.info(f"Downloaded {name} ({downloaded / 1024 / 1024:.1f}MB) in {elapsed:.1f}s")
            return dest

        except (requests.RequestException, ValueError, OSError) as e:
            # A partial file must not be left behind: the data directory is
            # scanned for work, so a truncated download would be ingested.
            self.logger.error(f"Download failed for {sanitize_url(url)}: {e}")
            if os.path.exists(dest):
                os.remove(dest)
            raise

    def _get_following_redirects(self, url: str) -> requests.Response:
        """GET ``url``, following up to MAX_REDIRECTS redirects by hand.

        Previously the request used allow_redirects=False and then checked
        ``resp.history`` — which is always empty in that mode — so the redirect
        check was dead code and a 3xx response body was saved as the data file.
        Each hop is now re-validated against the SSRF policy before it is
        fetched, and the connected peer is checked against private ranges.
        """
        original_host = (urlparse(url).hostname or '').lower()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            validate_remote_url(current)
            session = self.session
            if (urlparse(current).hostname or '').lower() != original_host:
                # Do not hand our credentials to whatever host a redirect names.
                # A bare session rather than header overrides: cookies set via
                # set_cookies are not domain-scoped, so the shared session would
                # still send them to the new host.
                session = requests.Session()
                session.headers.update({k: v for k, v in self.session.headers.items()
                                        if k.lower() != 'authorization'})
                session.proxies = dict(self.session.proxies)
                session.trust_env = self.session.trust_env
            resp = session.get(current, stream=True, timeout=300, allow_redirects=False)

            # Behind an HTTP(S) proxy the peer is the proxy itself, often on a
            # private address, and the proxy does the resolving — skip there.
            proxied = bool(self.session.proxies) or bool(
                self.session.trust_env and requests.utils.get_environ_proxies(current))
            peer = None if proxied else _peer_ip(resp)
            if peer is not None and _is_blocked_ip(peer):
                resp.close()
                raise ValueError(
                    f"Connected to a private/internal address ({peer}) for {sanitize_url(current)}; "
                    "possible DNS rebinding")

            if resp.status_code in _REDIRECT_CODES:
                location = resp.headers.get('Location')
                resp.close()
                if not location:
                    raise requests.HTTPError(
                        f"Redirect ({resp.status_code}) without a Location header: {sanitize_url(current)}")
                current = urljoin(current, location)
                self.logger.info(f"Following redirect to {sanitize_url(current)}")
                continue

            if not 200 <= resp.status_code < 300:
                resp.close()
                # raise_for_status ignores 1xx/3xx, which would otherwise be
                # saved as the file.
                resp.raise_for_status()
                raise requests.HTTPError(
                    f"Unexpected status {resp.status_code} for {sanitize_url(current)}", response=resp)
            return resp

        raise requests.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects: {sanitize_url(url)}")

    def download_batch(self, urls: list[str], filenames: Optional[list[str]] = None) -> list[str]:
        """Download multiple RDS files in sequence."""
        paths = []
        for i, url in enumerate(urls):
            name = filenames[i] if filenames and i < len(filenames) else None
            paths.append(self.download(url, filename=name))
        return paths


def download_and_process(url: str, filename: Optional[str] = None,
                         data_dir: Optional[str] = None,
                         token: Optional[str] = None,
                         cookies: Optional[dict] = None,
                         headers: Optional[dict] = None,
                         process: bool = True,
                         batch_size: int = 2000,
                         chunk_size: int = 10000,
                         logger=None) -> str:
    """
    Download an RDS file and optionally process it into the database.

    Args:
        url: Remote URL of the RDS file
        filename: Optional custom filename
        data_dir: Directory to save the file (defaults to static/data)
        token: Optional Bearer token for authentication
        cookies: Optional cookies dict for session auth
        headers: Optional custom HTTP headers
        process: If True, run housekeeping on the downloaded file
        batch_size: Batch size for DB insertion (if processing)
        chunk_size: Chunk size for RDS parsing (if processing)
        logger: Optional logger instance (uses module logger if not provided)

    Returns:
        Path to the downloaded file
    """
    downloader = RDSDownloader(data_dir=data_dir, logger=logger)
    if token:
        downloader.set_bearer_token(token)
    if cookies:
        downloader.set_cookies(cookies)
    if headers:
        downloader.set_headers(headers)

    filepath = downloader.download(url, filename=filename)

    if process:
        from app.services.housekeeper import process_file
        process_file(filepath, batch_size=batch_size, chunk_size=chunk_size)

    return filepath


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download RDS files from remote sources")
    parser.add_argument("urls", nargs="+", help="Remote RDS file URL(s)")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Directory to save files")
    parser.add_argument("--token", help="Bearer token for authentication")
    parser.add_argument("--cookie", action="append", help="Cookie in KEY=VALUE format (repeatable)")
    parser.add_argument("--header", action="append", help="Header in KEY:VALUE format (repeatable)")
    parser.add_argument("--process", action="store_true", help="Process downloaded files into the database")
    parser.add_argument("--batch-size", type=int, default=2000, help="Batch size for DB inserts")
    parser.add_argument("--chunk-size", type=int, default=10000, help="Chunk size for RDS parsing")

    args = parser.parse_args()

    cookies = {}
    if args.cookie:
        for c in args.cookie:
            k, _, v = c.partition("=")
            cookies[k] = v

    headers = {}
    if args.header:
        for h in args.header:
            k, _, v = h.partition(":")
            headers[k.strip()] = v.strip()

    for url in args.urls:
        download_and_process(
            url,
            data_dir=args.data_dir,
            token=args.token,
            cookies=cookies or None,
            headers=headers or None,
            process=args.process,
            batch_size=args.batch_size,
            chunk_size=args.chunk_size,
        )
