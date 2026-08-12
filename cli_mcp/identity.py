"""Forwarded-identity handling for deployments that terminate auth in a proxy.

The server does no authentication of its own and this module does not add any.
It reads an identity that something in front — an authenticating reverse proxy —
has already established, so the identity can be *recorded*: in the response
envelope and in the log line for every tool call. Nothing here gates on *who*
the caller is; that is an authorization decision and it stays with the deployment.

What it does gate on is whether the identity is trustworthy at all:

  * **The proxy header.** If `proxy_header` is configured, a request that does
    not carry the shared secret is refused. On a unix-socket deployment this is
    belt-and-braces — filesystem permissions already decide who can connect —
    and it is deliberately kept because the day somebody adds a TCP bind to
    debug something, the socket stops being the protection and this becomes the
    only thing standing between the tools and the network. Without it that
    change is quietly open; with it, it fails closed.

  * **An empty identity is a refusal, never a value.** With `require: true`, a
    missing or blank identity header returns 403. It is *not* recorded as
    "unknown" and allowed through. A proxy misconfigured to stop setting the
    header is the likeliest way this breaks, and it breaks in the direction of
    everything-still-working: every call succeeds, every record says "unknown",
    and nothing looks wrong. "We do not know who asked" and "nobody asked" must
    not render the same.

  * **A repeated header is a refusal.** HTTP allows a header to appear more than
    once. If it does, there is no single answer to who is asking, and picking
    the first is a choice made on the caller's behalf about a security-relevant
    value.

Configuration lives under `server.identity`; absent, the whole mechanism is off
and identity is `None` everywhere, which is the behaviour of every release
before this one.
"""

import hmac
import os
from dataclasses import dataclass


class IdentityRefused(Exception):
    """A request whose identity cannot be established. Carries an HTTP status."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class IdentityMisconfigured(Exception):
    """Raised at load time, so the server refuses to start rather than serving
    with an identity check that silently is not running."""


@dataclass(frozen=True)
class IdentityConfig:
    header: str
    require: bool = True
    proxy_header: str | None = None
    proxy_secret: str | None = None
    bind_to_session: bool = True

    @classmethod
    def from_config(cls, server_cfg: dict | None) -> "IdentityConfig | None":
        """Build from the `server.identity` block. Returns None when absent.

        Raises IdentityMisconfigured rather than degrading. A deployment that
        asks for a proxy secret and does not supply one must not start and
        quietly accept every request instead -- that is the failure this whole
        module exists to make impossible.
        """
        cfg = (server_cfg or {}).get("identity")
        if not cfg:
            return None

        header = (cfg.get("header") or "").strip()
        if not header:
            raise IdentityMisconfigured(
                "server.identity is present but server.identity.header is empty; "
                "either name the header carrying the identity or remove the block"
            )

        proxy_header = (cfg.get("proxy_header") or "").strip() or None
        secret_env = (cfg.get("proxy_secret_env") or "").strip() or None
        proxy_secret = None

        if proxy_header:
            if not secret_env:
                raise IdentityMisconfigured(
                    f"server.identity.proxy_header is set to {proxy_header!r} but "
                    "server.identity.proxy_secret_env names no environment variable "
                    "to read the expected value from"
                )
            proxy_secret = os.environ.get(secret_env) or ""
            if not proxy_secret:
                raise IdentityMisconfigured(
                    f"server.identity.proxy_secret_env names {secret_env!r}, which is "
                    "unset or empty in this process's environment. Refusing to start: "
                    "an unset secret would otherwise mean every request is accepted."
                )

        return cls(
            header=header,
            require=bool(cfg.get("require", True)),
            proxy_header=proxy_header,
            proxy_secret=proxy_secret,
            bind_to_session=bool(cfg.get("bind_to_session", True)),
        )


def _sole_value(headers, name: str) -> str | None:
    """The header's single value, or None if absent. Refuses duplicates."""
    values = headers.getlist(name)
    if not values:
        return None
    if len(values) > 1:
        raise IdentityRefused(
            403, f"{name} appeared {len(values)} times; refusing an ambiguous identity"
        )
    return values[0]


def resolve_identity(headers, config: IdentityConfig | None) -> str | None:
    """Return the caller's identity, or raise IdentityRefused.

    `headers` is anything with a `getlist(name)` returning a list of values --
    starlette's Headers, which is case-insensitive, as HTTP requires.

    Returns None only when the mechanism is switched off, or when it is on with
    `require: false` and no identity was presented. None means "not established"
    and is rendered as JSON null, never as a placeholder string.
    """
    if config is None:
        return None

    if config.proxy_header is not None:
        presented = _sole_value(headers, config.proxy_header) or ""
        # compare_digest over the encoded form: it raises on non-ASCII str.
        if not hmac.compare_digest(presented.encode(), (config.proxy_secret or "").encode()):
            raise IdentityRefused(
                403,
                f"{config.proxy_header} missing or wrong; this request did not arrive "
                "through the configured proxy",
            )

    identity = (_sole_value(headers, config.header) or "").strip()

    if not identity:
        if config.require:
            raise IdentityRefused(
                403,
                f"{config.header} is empty. The proxy is expected to set it on every "
                "route; refusing rather than recording this call as an unknown caller.",
            )
        return None

    return identity
