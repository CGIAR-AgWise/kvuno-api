"""PROXY_FIX_HOPS.

Without ProxyFix, get_remote_address() returns the reverse proxy's own IP, so
every user shares a single rate-limit bucket — one user can exhaust the quota
for everyone. The app sits behind the nginx container that serves the SPA, which
forwards X-Forwarded-For, so the default is now 1.

The risk is the mirror image: ProxyFix trusts the header unconditionally, so an
app exposed with no proxy in front can be rate-limit-evaded by forging it. Hence
the documented 0 override.
"""

import os
from unittest.mock import patch

import pytest
from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix


def _client(hops):
    probe = Flask("probe")

    @probe.route("/who")
    def who():
        return {"ip": request.remote_addr}

    if hops:
        probe.wsgi_app = ProxyFix(probe.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
    return probe.test_client()


class TestDefault:
    def test_enabled_by_default(self):
        from app import create_app
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROXY_FIX_HOPS", None)
            app = create_app()
        assert isinstance(app.wsgi_app, ProxyFix), (
            "without ProxyFix every user shares one rate-limit bucket"
        )

    def test_clients_are_distinguished_through_the_wsgi_stack(self):
        """Must go through WSGI: test_request_context bypasses the middleware."""
        c = _client(1)
        ips = {
            c.get('/who', headers={'X-Forwarded-For': ip}).get_json()['ip']
            for ip in ('203.0.113.9', '198.51.100.4', '192.0.2.77')
        }
        assert len(ips) == 3

    def test_without_the_fix_all_clients_look_identical(self):
        c = _client(0)
        ips = {
            c.get('/who', headers={'X-Forwarded-For': ip}).get_json()['ip']
            for ip in ('203.0.113.9', '198.51.100.4', '192.0.2.77')
        }
        assert len(ips) == 1, 'expected the shared-bucket failure this setting prevents'


class TestOverride:
    @pytest.mark.parametrize('value,expected', [('0', False), ('1', True), ('2', True)])
    def test_value_is_honoured(self, value, expected):
        from app import create_app
        with patch.dict(os.environ, {'PROXY_FIX_HOPS': value}):
            app = create_app()
        assert isinstance(app.wsgi_app, ProxyFix) is expected

    def test_non_numeric_is_a_configuration_error(self):
        # It used to be ignored silently, leaving the app on a setting the
        # operator did not choose. Every invalid setting now stops startup.
        from app import create_app
        from app.config import ConfigurationError
        with patch.dict(os.environ, {'PROXY_FIX_HOPS': 'nonsense'}):
            with pytest.raises(ConfigurationError, match='PROXY_FIX_HOPS'):
                create_app()
