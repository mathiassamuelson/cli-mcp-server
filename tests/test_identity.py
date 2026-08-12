"""Forwarded-identity resolution: the unit-level rules.

The refusals are the point of this module, so each is asserted on its own
rather than through "the request failed somehow". See test_e2e_identity.py for
the same rules over the real transport, including session binding.
"""

import pytest
from starlette.datastructures import Headers

from cli_mcp.identity import (
    IdentityConfig,
    IdentityMisconfigured,
    IdentityRefused,
    resolve_identity,
)


def headers(**kwargs) -> Headers:
    # starlette matches `raw` keys byte-for-byte against a lowercased lookup,
    # so raw names must be lowercase -- HTTP itself is case-insensitive and
    # test_header_lookup_is_case_insensitive covers that end.
    return Headers(
        raw=[(k.replace("_", "-").lower().encode(), v.encode()) for k, v in kwargs.items()]
    )


def cfg(**kwargs) -> IdentityConfig:
    kwargs.setdefault("header", "X-Auth-Request-Email")
    return IdentityConfig(**kwargs)


# --- the mechanism switched off ------------------------------------------


def test_no_config_yields_no_identity():
    assert resolve_identity(headers(), None) is None


def test_absent_identity_block_is_none():
    assert IdentityConfig.from_config({"node_name": "n"}) is None
    assert IdentityConfig.from_config(None) is None
    assert IdentityConfig.from_config({}) is None


# --- require: the empty identity is a refusal, not a value ---------------


def test_missing_identity_refused_when_required():
    with pytest.raises(IdentityRefused) as exc:
        resolve_identity(headers(), cfg(require=True))
    assert exc.value.status == 403


def test_blank_identity_refused_when_required():
    """A header set to the empty string is the shape a misconfigured
    auth_request_set produces -- present, and carrying nothing."""
    with pytest.raises(IdentityRefused):
        resolve_identity(headers(X_Auth_Request_Email="   "), cfg(require=True))


def test_missing_identity_is_none_when_not_required():
    assert resolve_identity(headers(), cfg(require=False)) is None


def test_identity_never_defaults_to_a_placeholder():
    """Whatever happens, no call resolves to a string nobody authenticated."""
    with pytest.raises(IdentityRefused):
        resolve_identity(headers(), cfg(require=True))
    assert resolve_identity(headers(), cfg(require=False)) is None


def test_identity_is_returned_and_stripped():
    got = resolve_identity(headers(X_Auth_Request_Email=" a@b.com "), cfg())
    assert got == "a@b.com"


def test_header_lookup_is_case_insensitive():
    raw = Headers(raw=[(b"x-auth-request-email", b"a@b.com")])
    assert resolve_identity(raw, cfg(header="X-Auth-Request-Email")) == "a@b.com"


# --- duplicate headers ----------------------------------------------------


def test_duplicate_identity_header_refused():
    raw = Headers(raw=[
        (b"x-auth-request-email", b"real@b.com"),
        (b"x-auth-request-email", b"forged@b.com"),
    ])
    with pytest.raises(IdentityRefused) as exc:
        resolve_identity(raw, cfg())
    assert exc.value.status == 403
    assert "ambiguous" in exc.value.reason


# --- the proxy secret -----------------------------------------------------


def test_proxy_secret_required_when_configured():
    c = cfg(proxy_header="X-Trail-Proxy", proxy_secret="s3cret")
    with pytest.raises(IdentityRefused) as exc:
        resolve_identity(headers(X_Auth_Request_Email="a@b.com"), c)
    assert exc.value.status == 403


def test_wrong_proxy_secret_refused():
    c = cfg(proxy_header="X-Trail-Proxy", proxy_secret="s3cret")
    with pytest.raises(IdentityRefused):
        resolve_identity(
            headers(X_Auth_Request_Email="a@b.com", X_Trail_Proxy="wrong"), c
        )


def test_correct_proxy_secret_passes():
    c = cfg(proxy_header="X-Trail-Proxy", proxy_secret="s3cret")
    got = resolve_identity(
        headers(X_Auth_Request_Email="a@b.com", X_Trail_Proxy="s3cret"), c
    )
    assert got == "a@b.com"


def test_proxy_secret_checked_before_identity():
    """A request off the proxy is refused for that reason, not for its
    identity -- otherwise the message tells an unauthenticated caller which
    header to add next."""
    c = cfg(proxy_header="X-Trail-Proxy", proxy_secret="s3cret")
    with pytest.raises(IdentityRefused) as exc:
        resolve_identity(headers(), c)
    assert "X-Trail-Proxy" in exc.value.reason


def test_non_ascii_secret_does_not_crash():
    """hmac.compare_digest raises TypeError on non-ASCII str; comparing the
    encoded form keeps a odd header from becoming a 500."""
    c = cfg(proxy_header="X-Trail-Proxy", proxy_secret="s3cret")
    with pytest.raises(IdentityRefused):
        resolve_identity(
            headers(X_Auth_Request_Email="a@b.com", X_Trail_Proxy="pässwörd"), c
        )


# --- misconfiguration stops the server ------------------------------------


def test_unset_secret_env_refuses_to_load(monkeypatch):
    monkeypatch.delenv("CLI_MCP_TEST_SECRET", raising=False)
    with pytest.raises(IdentityMisconfigured) as exc:
        IdentityConfig.from_config({
            "identity": {
                "header": "X-Auth-Request-Email",
                "proxy_header": "X-Trail-Proxy",
                "proxy_secret_env": "CLI_MCP_TEST_SECRET",
            }
        })
    assert "CLI_MCP_TEST_SECRET" in str(exc.value)


def test_empty_secret_env_refuses_to_load(monkeypatch):
    monkeypatch.setenv("CLI_MCP_TEST_SECRET", "")
    with pytest.raises(IdentityMisconfigured):
        IdentityConfig.from_config({
            "identity": {
                "header": "X-Auth-Request-Email",
                "proxy_header": "X-Trail-Proxy",
                "proxy_secret_env": "CLI_MCP_TEST_SECRET",
            }
        })


def test_proxy_header_without_secret_env_refuses_to_load():
    with pytest.raises(IdentityMisconfigured):
        IdentityConfig.from_config({
            "identity": {
                "header": "X-Auth-Request-Email",
                "proxy_header": "X-Trail-Proxy",
            }
        })


def test_empty_header_name_refuses_to_load():
    with pytest.raises(IdentityMisconfigured):
        IdentityConfig.from_config({"identity": {"require": True}})


def test_secret_is_read_from_env_at_load(monkeypatch):
    monkeypatch.setenv("CLI_MCP_TEST_SECRET", "from-env")
    c = IdentityConfig.from_config({
        "identity": {
            "header": "X-Auth-Request-Email",
            "proxy_header": "X-Trail-Proxy",
            "proxy_secret_env": "CLI_MCP_TEST_SECRET",
        }
    })
    assert c.proxy_secret == "from-env"
    assert c.require is True
    assert c.bind_to_session is True


def test_defaults_are_the_strict_ones():
    c = IdentityConfig.from_config({"identity": {"header": "X-Id"}})
    assert c.require is True and c.bind_to_session is True
