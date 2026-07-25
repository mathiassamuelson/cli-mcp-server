# Proposal: authentication, authorization, and transport security

**Status: proposal. Nothing here is implemented.** No behaviour changes ship
with this document. It exists to get the shape agreed before code is written,
and to record what was verified versus what still needs a spike.

Two dated facts, because they move: the installed SDK is `mcp` 1.28.1
(`LATEST_PROTOCOL_VERSION = "2025-11-25"`), and the MCP `2026-07-28`
specification is a release candidate at the time of writing. §8 covers what
that revision changes for this design, and it is not cosmetic.

---

## 1. Where we are today

- The server binds `0.0.0.0:8100` over plaintext HTTP with no authentication.
- Any party that can open a TCP connection to the port gets the **entire**
  catalog. The allow/deny filter is per *tool*, never per *caller*.
- The audit log (added in the previous change) already carries a `principal`
  object, permanently `{"authenticated": false, …}`. The placeholder was put
  there so that filling it in is a value change rather than a schema change.
- `README.md` already tells operators to "wrap it behind a VPN, a reverse
  proxy with mTLS, or bind to localhost." This proposal is about making that
  advice into something the server participates in rather than merely hopes
  for.

The gap is not only "there is no login." It is that **the server cannot
distinguish callers at all**, so there is nothing to write an authorization
policy against and nothing truthful to write in the audit log.

---

## 2. Three decisions that are usually conflated

Keeping these apart is most of the work:

| | Question | Can it be delegated to a proxy? |
|---|---|---|
| **Transport security** | Is the channel confidential and integrity-protected? | **Yes** — this is what proxies are for. |
| **Authentication** | Who is the caller? | **Yes, partly** — but the answer must reach the app trustworthily. |
| **Authorization** | Which tools may *this* caller invoke? | **No.** See below. |

The architectural line this proposal draws:

> **Authentication is pluggable and may be delegated. Authorization is never
> delegated.**

Authorization stays in `cli_mcp/`, next to the filter that is already the
security boundary, for three reasons. It needs the catalog, which the proxy
cannot see. It must produce the same audit records as every other denial. And
it must fail closed in the same way — a proxy that is bypassed or
misconfigured should cost you authentication, not the entire policy.

---

## 3. Constraints this codebase actually imposes

These are load-bearing and were verified, not assumed.

**The audit principal is connection-scoped.** `PRINCIPAL` is a `ContextVar`
set in `handle_sse`. A tool call does not execute in the task of the POST that
carried it, so a value set in `handle_messages` is invisible to `call_tool`.
Any authentication design that wants per-request identity under the current
SSE transport hits this wall.

**The SDK already binds session ownership to the credential.** In
`mcp/server/sse.py`, `connect_sse` stores `authorization_context(user)` in
`_session_owners[session_id]`, and `handle_post_message` rejects a POST whose
principal differs — responding exactly as if the session did not exist. So
once authentication exists, connection-scoped attribution is *sound* rather
than merely convenient: the SDK guarantees that every message on a session
comes from the principal that opened it. This is worth knowing before anyone
proposes re-plumbing attribution to be per-request under SSE.

**`AuthorizationContext` is the SDK's principal shape** — `{client_id,
issuer, subject}` — and it is what the session binding compares. Our
`principal` object should be a superset of it, not a parallel invention.

**Downstream forks.** `cli_mcp/` copies across unchanged; `pyproject.toml`,
`bin/server.sh`, `configs/`, `CLAUDE.md` are re-derived. Auth *code* must live
in `cli_mcp/` to port cheaply. Auth *config* lands in `configs/`, which is
already build-coupled — no new coupled file, provided we do not put policy in
a fifth location.

**Akamai's requirement.** mTLS between 12–20 distributed service nodes inside
one product, principal from the certificate. This is a closed network with a
private CA and no human in the loop — which rules out interactive OAuth as the
*primary* mechanism and makes machine identity the right model.

---

## 4. How MCP servers actually do this

Grounded in the spec text rather than folklore.

**Authorization is OPTIONAL in MCP.** The spec's own words: "Authorization is
**OPTIONAL** for MCP implementations." HTTP transports **SHOULD** conform to
the OAuth profile; stdio transports **SHOULD NOT** and take credentials from
the environment; alternative transports **MUST** follow established security
best practice for their protocol. A closed mTLS deployment is therefore not
"non-compliant" — it is the third case. The cost is interoperability, not
conformance: a generic MCP client cannot present a certificate from a private
CA and expects the OAuth profile instead (§12, question 4).

**When servers do implement the profile, they are OAuth 2.1 Resource
Servers** — not authorization servers. The normative requirements that land on
*us* are narrow:

- **MUST** implement OAuth 2.0 Protected Resource Metadata (RFC 9728), served
  at `/.well-known/oauth-protected-resource{/path}`.
- **MUST** validate that a presented token was issued *for this server* —
  audience binding per RFC 8707. Accepting a token minted for another service
  is the single most common MCP auth vulnerability.
- **MUST NOT** pass a received token through to any upstream API.
- **MUST** return 401 for missing/invalid tokens, 403 for insufficient scope,
  with a `WWW-Authenticate` header carrying `resource_metadata` and the
  required `scope`.
- Tokens arrive in `Authorization: Bearer …` on **every** request, never in a
  query string.

The authorization server itself is explicitly out of scope for us — Keycloak,
Okta, an internal AS, whatever the deployment already runs.

**The SDK ships most of this already.** In `mcp.server.auth`: the
`TokenVerifier` protocol (a single `async verify_token(token) -> AccessToken |
None`), `BearerAuthBackend`, `RequireAuthMiddleware` (scope enforcement and
correctly-formed `WWW-Authenticate` errors), `AuthContextMiddleware`, and
route builders for RFC 9728 metadata. Adopting the OAuth path is mostly
assembly, not construction.

**In practice deployments fall into three tiers:**

1. **stdio / local** — no transport auth; the client owns the process and
   credentials come from the environment. Not our case.
2. **Closed internal network** — mTLS, network isolation, or a static shared
   token. Machine-to-machine, no user present. **This is Akamai's tier.**
3. **Public or multi-tenant** — the full OAuth 2.1 profile, because arbitrary
   clients must be able to discover how to authenticate.

A server can support tier 2 and tier 3 simultaneously; they are different
backends producing the same principal.

---

## 5. Option A — NGINX front end

NGINX terminates TLS and mTLS; the app listens only on loopback or a Unix
socket and learns the caller's identity from headers the proxy sets.

```nginx
server {
    listen 8443 ssl;
    ssl_certificate         /etc/pki/mcp/node.pem;
    ssl_certificate_key     /etc/pki/mcp/node.key;

    ssl_client_certificate  /etc/pki/mcp/internal-ca.pem;   # trust anchor
    ssl_verify_client       on;                              # reject unverified
    ssl_verify_depth        2;
    ssl_crl                 /etc/pki/mcp/internal-crl.pem;

    location /mcp {
        proxy_pass http://unix:/run/cli-mcp-server.sock;

        # Always set, therefore always overwriting anything the client sent.
        proxy_set_header X-MCP-Client-Verify  $ssl_client_verify;
        proxy_set_header X-MCP-Client-DN      $ssl_client_s_dn;
        proxy_set_header X-MCP-Client-Serial  $ssl_client_serial;
        proxy_set_header X-MCP-Client-FP      $ssl_client_fingerprint;

        # SSE will not work without these three.
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

### The failure mode that matters

Header-borne identity is only as good as the guarantee that **nothing can
reach the app except through the proxy**. If the app is still listening on
`0.0.0.0:8100`, an attacker connects directly and sets `X-MCP-Client-DN` to
whatever they like. The proxy is then decorative.

This is not hypothetical — it is the standard way header-based auth fails, and
it fails *silently*, because everything looks correct from the outside.

Mitigations, all of which should be designed in rather than documented:

1. **Bind to a Unix socket, or loopback only.** A Unix socket is strictly
   better: it cannot be reached from another host at all, and filesystem
   permissions become the access control.
2. **Refuse to start** in `mtls-proxy-headers` mode if the configured bind
   address is not loopback or a Unix socket. The server can detect its own
   misconfiguration; it should.
3. **Require `X-MCP-Client-Verify: SUCCESS`** and reject anything else. NGINX
   sets this to `SUCCESS`, `NONE`, or `FAILED:<reason>`.
4. **Log the enforcement mode in the audit startup record**, so the log states
   which trust regime was in force rather than leaving it to be inferred.

### Parsing the DN is its own trap

`$ssl_client_s_dn` is RFC 2253 format on nginx ≥ 1.11.6 (escaped, and in
reverse order from the legacy `$ssl_client_s_dn_legacy`). A CN containing a
comma arrives as `CN=svc\,node-a`, so naive `split(",")` then `split("=")`
silently yields the wrong principal — and getting the principal wrong on a
security boundary is worse than failing to get one.

Recommendation: treat the **certificate fingerprint or (issuer, serial) pair
as the stable identity key**, and CN as a human-readable label carried
alongside it. Policy then keys off something that cannot be mangled by string
parsing, while the audit log still reads well. If CN must be the key, the
parser needs RFC 2253 unescaping and a test suite of adversarial DNs.

### Pros

- **We implement no cryptography and own no TLS policy.** Protocol versions,
  cipher suites, renegotiation, session resumption, OCSP stapling, CRL
  refresh — all handled by software that is far more reviewed than anything we
  would write, and by an ops team that already knows how to operate it.
- **Certificate and CRL rotation is a proxy reload**, not an application
  restart. On 12–20 nodes that difference is real.
- **Rate limiting, connection caps, request size limits, and slowloris
  protection come free** — none of which this server has today.
- **No new Python dependencies.** `cli_mcp/` stays pure and ports unchanged.
- One proxy can front several services on a node.
- Very likely matches infrastructure Akamai already runs.

### Cons

- **A second moving part on every node**, with a security-relevant config
  file. Across 12–20 nodes, config drift is a genuine risk, and a node whose
  `ssl_verify_client` quietly reads `optional` is invisible from the app side.
- **The trust boundary is a footgun** (above). It can be made safe, but it has
  to be made safe deliberately.
- **Identity is stringly-typed through headers**, with the DN-parsing hazard
  above.
- **Two logs, not one.** The nginx access log and our audit log are separate
  and correlate only if a request id is threaded through. Our audit log alone
  can no longer tell you the TLS parameters of the connection.
- Does nothing for the OAuth path: **RFC 9728 metadata, audience validation
  and `WWW-Authenticate` challenges are protocol-level and cannot be done by a
  proxy.**

---

## 6. Option B — build it into the server

### B1: in-process mTLS

**Verified, and it constrains the answer:** uvicorn 0.51.0 *can* terminate
mTLS — `ssl_certfile`, `ssl_keyfile`, `ssl_ca_certs`, `ssl_cert_reqs` are all
supported — but it does **not** implement the ASGI TLS extension. There is no
`scope["extensions"]["tls"]`, and the strings `peercert` and `ssl_object` do
not appear anywhere in the package. Confirmed by grep against the installed
version.

So uvicorn will happily *enforce* client certificates while giving the
application no supported way to *read* them. Recovering the peer certificate
means a custom protocol class reaching
`transport.get_extra_info("ssl_object").getpeercert()` and injecting it into
the scope — i.e. depending on uvicorn internals, on a security boundary, in a
project whose forks must port this by hand.

**This is the strongest single argument against in-process mTLS in this
stack**, and it needs a spike before anyone commits to it (§11).

### B2: in-process OAuth 2.1 Resource Server

Materially easier, because bearer tokens arrive in a header that ASGI already
exposes. Assembly of SDK parts:

- a `TokenVerifier` implementation (JWT validation against the AS's JWKS, or
  RFC 7662 introspection);
- `BearerAuthBackend` + Starlette's `AuthenticationMiddleware`;
- `RequireAuthMiddleware` for scope enforcement and spec-correct errors;
- the SDK's RFC 9728 route builder for protected-resource metadata;
- audience validation against our canonical resource URI — the one thing to
  get right by hand, and the most commonly botched requirement in the
  ecosystem.

### Pros

- **One process, one config file, one thing to deploy and keep consistent
  across 12–20 nodes.** No trust-boundary-by-header.
- **Principal extraction is typed**, straight from a certificate or token
  object — no DN string parsing.
- **The OAuth path essentially must live here anyway.** A proxy cannot serve
  RFC 9728 metadata about our resource identity, cannot validate audience
  against our canonical URI, and cannot emit the right `WWW-Authenticate`
  challenge for an MCP client to act on.
- **Fits the fork story**: it is all `cli_mcp/`, which ports unchanged.
- **Testable the way this repo tests things** — generate a throwaway CA and
  leaf certs under `tmp_path`, no mocks, assert on real handshakes.
- The audit log can record TLS and token facts in the same record as the
  decision.

### Cons

- **We own TLS policy**: versions, ciphers, renegotiation, and keeping those
  current. Python's `ssl` is capable, but the policy and its maintenance
  become ours.
- **Rotation is harder.** Reloading a CA bundle or CRL without dropping
  connections needs code that nginx already has.
- **The uvicorn peer-certificate gap above.**
- **No rate limiting or slowloris protection**, and adding them is out of
  scope for this project.
- More security-relevant Python to review, in a repo whose stated value is
  that the risky surface is small.

---

## 7. Running without TLS, on purpose

There is a legitimate need to run authentication and authorization over plain
HTTP: internal testing, CI, and demonstrating that the policy layer works
without standing up a PKI first. The design should support it deliberately
rather than leave people to arrive at it by omitting configuration.

### "HTTP is insecure" is too coarse a rule

What matters is whether the channel is **confined**, not what the URL scheme
says. The NGINX design in §5 already carries plaintext HTTP on its backend
hop — over a Unix socket, where there is no network to intercept and
filesystem permissions are the access control. That hop is not a weakness.
Plaintext across a routable network is a weakness no matter how good the
authentication in front of it is.

So transport posture is its own axis, declared explicitly rather than
inferred:

```yaml
transport:
  posture: tls-direct | tls-upstream | confined | insecure-testing
```

| Posture | Channel | Verdict |
|---|---|---|
| `tls-direct` | Server terminates TLS | Secure |
| `tls-upstream` | Proxy terminated TLS; our hop is a Unix socket or loopback | Secure — Option A, the Akamai default |
| `confined` | No TLS, bound to a Unix socket or loopback, no proxy | Secure for same-host clients |
| `insecure-testing` | No TLS on a routable address | **Not secure. Testing only.** |

### What still works over plaintext, and what it actually proves

Every backend works *mechanically*, because none of the authentication logic
depends on TLS — the credential arrives in a header either way. What plaintext
removes is confidentiality and integrity, which happen to be the properties
that make the credential mean anything:

- **`static-token` and `oauth-bearer`** — the bearer credential is readable
  and replayable by anyone on the path. Authentication becomes theatre; it
  will pass tests and stop nobody.
- **`mtls-proxy-headers` without the proxy in front of it** — the identity
  headers are entirely caller-supplied. This is not weak authentication, it is
  *none*: the caller simply names itself.

What it does still demonstrate is real, and worth having:

- the policy engine denies the right tools for a given principal;
- audit records carry the right principal and the right decision;
- 401 and 403 responses have the spec-correct shape, including
  `WWW-Authenticate` and scope challenges;
- scope enforcement fires where it should.

That is genuine functional and integration coverage. It is not evidence that
the deployment is secure, and the design should make that impossible to
confuse.

### Guardrails

1. **Never inferable.** `insecure-testing` must be named in config. The
   *absence* of TLS settings must never be read as "no TLS is fine" — that is
   how a test posture reaches production.
2. **Refuse the dangerous combination unless it is asserted twice.**
   `insecure-testing` on a routable bind additionally requires an environment
   variable — `CLI_MCP_ALLOW_INSECURE=1` — not just a config key. Config files
   get copied between environments; environment variables do not travel with
   them. This is the cheapest available barrier against a test config becoming
   a production one.
3. **Loud at startup**: a stderr banner, plus `transport.posture` in the audit
   startup record, so the log states the posture that was in force rather than
   leaving it to be inferred.
4. **Honest in every record, not just at startup.** Each `decision` record's
   principal carries the channel it was authenticated over:

   ```json
   "principal": {"authenticated": true, "channel": "unprotected",
                 "method": "header-identity-unverified", "subject": "CN=test-client"}
   ```

   A principal asserted over an unprotected channel must not be recorded
   identically to one authenticated over TLS, or the audit log overstates what
   it knows. This is the same reasoning that put `authenticated: false` in the
   schema from day one: the field exists so a reader cannot mistake silence
   for a guarantee.
5. **Name the weak backend for what it is.** The plaintext identity backend
   should be `header-identity-unverified`, *not* `mtls-proxy-headers` with a
   flag. A config file should never read like production mTLS while being
   nothing of the sort. Naming is a safety feature here — it is what someone
   skimming a diff will see.

### A benefit, not only a concession

This also makes the test suite better. Phase 3 (policy scoping, §10) can be
exercised end to end over plain HTTP with no certificate fixtures at all —
principal in, policy applied, audit record out. Certificate fixtures are then
only needed for the narrow tests that parse DNs and validate chains, which is
where they earn their cost. Given this repo's no-mocks convention, that is the
difference between policy tests that build a throwaway CA and policy tests
that build a dict.

---

## 8. The 2026-07-28 specification changes this

Timely enough to design around rather than discover later. In the release
candidate:

- **The protocol core becomes stateless.** The `initialize` handshake and the
  `Mcp-Session-Id` header are eliminated; protocol metadata moves into `_meta`
  so any instance can serve any request behind a round-robin load balancer.
- **SSE streams are replaced by Multi Round-Trip Requests.** Servers return
  `InputRequiredResult` objects that clients resubmit, removing the need for a
  persistent connection.
- Streamable HTTP gains required `Mcp-Method` and `Mcp-Name` routing headers.
- Six SEPs harden authorization: `iss` validation per RFC 9207 (SEP-2468),
  `application_type` in DCR (SEP-837), credential-to-AS binding (SEP-2352),
  refresh tokens (SEP-2207), scope accumulation on step-up (SEP-2350),
  `.well-known` suffix handling (SEP-2351).
- MCP's own `logging` capability enters deprecation in favour of **stderr or
  OpenTelemetry** — which independently validates the audit sink's stderr
  default.

Three consequences for this proposal:

1. **Do not build anything on session identity.** The SDK's
   `_session_owners` credential binding — genuinely useful today, §3 — becomes
   moot when sessions do. A design that leans on it ages badly within months.
2. **Attribution becomes per-request, which is strictly better.** The
   connection-scoped limitation documented in the audit work is an artefact of
   SSE, not something we need to design around permanently.
3. **Therefore: make principal extraction a pure function of the ASGI scope**
   — `scope -> Principal`. Under SSE today, call it once in `handle_sse` and
   stash it in the existing `ContextVar`. Under the stateless transport, call
   the same function per request. The extraction logic, the policy engine, and
   the audit schema all survive the transport change untouched; only the
   call site moves. This costs nothing now and is the difference between a
   transport migration touching one function or touching the whole auth layer.

---

## 9. Recommendation

**Split by concern rather than picking one option.**

| Concern | Where | Why |
|---|---|---|
| TLS + mTLS termination (Akamai) | **NGINX** (Option A) | We should not own TLS policy or certificate rotation across 12–20 nodes, and in-process mTLS needs uvicorn internals (§6 B1). |
| Transport posture declaration | **In-process** (§7) | The server should refuse unsafe combinations and state the posture in every record, rather than trusting that a proxy is really there. |
| OAuth 2.1 Resource Server mode | **In-process** (Option B2) | RFC 9728 metadata, audience validation and `WWW-Authenticate` are protocol-level; a proxy cannot do them. |
| Principal extraction | **In-process, always** | One `scope -> Principal` function, one audit schema, regardless of front end. |
| Authorization policy | **In-process, always** | Needs the catalog; must produce the same audit records; must fail closed independently of the proxy. |

Concretely: a pluggable `AuthNBackend` in `cli_mcp/auth.py` producing a single
`Principal` type, selected by config.

```yaml
transport:
  posture: tls-upstream          # see §7; never inferred from what is absent

auth:
  backend: none | mtls-proxy-headers | mtls-direct | oauth-bearer
           | static-token | header-identity-unverified

  # backend: mtls-proxy-headers
  proxy:
    require_bind_loopback: true       # refuse to start otherwise
    verify_header: X-MCP-Client-Verify
    dn_header: X-MCP-Client-DN
    fingerprint_header: X-MCP-Client-FP
    identity_from: fingerprint | cn | san-uri   # default: fingerprint
    cn_label: true                    # keep CN as a human label either way

  # backend: oauth-bearer
  oauth:
    resource_url: https://node-a.internal:8443/mcp   # audience to enforce
    issuer_url: https://sso.internal/realms/mcp
    required_scopes: ["mcp:invoke"]
```

Backends, in the order I would build them:

1. **`none`** — today's behaviour, but *explicit* and stated in the startup
   record, so "unauthenticated" is a recorded choice rather than a silence.
2. **`mtls-proxy-headers`** — Akamai's path. Ships with the bind-address
   self-check and `require_bind_loopback` defaulting to `true`.
3. **`oauth-bearer`** — the generic, spec-conformant path for anyone outside a
   closed network. Mostly SDK assembly.
4. **`static-token`** — constant-time-compared shared secret. Genuinely useful
   for bootstrap, CI, and dev, and honest about being weak. Should log a
   warning at startup.
5. **`header-identity-unverified`** — the plaintext testing backend from §7.
   Trusts identity headers with nothing in front of them, and is named so that
   no config file using it can be mistaken for production mTLS. Worth building
   early despite being last in security value: it is what lets the policy
   layer (§10) be tested without certificate fixtures.
6. **`mtls-direct`** — only if the spike in §11 says the uvicorn path is
   maintainable.

### On CN specifically

Supported, since it is the stated requirement — but two things to weigh.

RFC 6125 deprecated CN in favour of subjectAltName for identity, and modern
internal PKI increasingly puts service identity in a SAN URI (`spiffe://…`) or
DNS name. Making `identity_from` configurable with `cn`, `san-uri`, and
`fingerprint` costs little now and avoids a migration later. I would default
to `fingerprint` for the *policy key* (unmanglable, survives CN renames) and
always carry CN as the human label in the audit record — but `cn` is
available if the existing PKI makes it the natural key.

---

## 10. Authorization: scoping the catalog to a principal

Authentication alone changes nothing about blast radius — an authenticated
caller still gets every tool. The policy layer is what makes authentication
worth having.

**Reuse the filter's existing shape rather than inventing a second policy
language.** The catalog filter is deny-first, then allow, then default-deny;
operators already know it, and a second grammar with different precedence is
how policy bugs happen.

```yaml
policy:
  - principal: "spiffe://prod/svc/monitoring"     # exact, or glob
    tools:
      deny:  ["kubectl*"]
      allow: ["ps", "dns.*"]
  - principal: "CN=oncall-*"
    tools:
      allow: ["*"]
```

Evaluated **before** the command filter, so an unauthorized tool is rejected
before its arguments are ever examined. A policy denial produces the same
audit shape as any other refusal — a `decision` record with no `outcome`,
which the existing conformance probe already pins.

**Back-compatibility, deliberately:** `policy` absent means every
authenticated principal gets the full catalog, preserving today's behaviour on
upgrade. `policy` present means default-deny within it. Which regime is in
force goes in the startup record, so it is never an invisible property of the
deployment — the same reasoning as `on_write_failure`.

---

## 11. Spikes needed before committing

Same discipline as the ContextVar spike that preceded the audit work: these
answer questions that cannot be settled by reading.

1. **Can uvicorn hand us a peer certificate without depending on internals?**
   Try `transport.get_extra_info("ssl_object").getpeercert()` via a custom
   protocol class; assess how much uvicorn internal surface it pins. Decides
   whether `mtls-direct` is viable at all. **Blocks §9 item 5 only.**
2. **Does the principal survive the SSE task boundary once it comes from
   middleware rather than `handle_sse`?** The existing spike verified a value
   set *in* `handle_sse`. Middleware wraps the ASGI call, so it should hold —
   but "should" is what the first spike disproved for `handle_messages`.
3. **What does `$ssl_client_s_dn` actually look like** for the real Akamai
   certificate profile, on the nginx version deployed? Decides how much RFC
   2253 unescaping is needed and whether CN is even unique across the fleet.
4. **Does an SSE stream survive the proxy config in §5** under a long-lived
   connection with an idle period? `proxy_buffering off` and
   `proxy_read_timeout` are necessary; whether they are sufficient is worth
   ten minutes of testing rather than a production incident.

---

## 12. Open questions for the deployment owners

1. **Is there an internal OAuth AS** available to these nodes, or is mTLS the
   only credential that exists? If the latter, `oauth-bearer` is a
   community-facing feature rather than something Akamai will ever enable —
   which changes its priority but not its design.
2. **One service identity for the fleet, or per-node identities?** Per-node
   certificates make the audit log far more useful and cost nothing extra at
   this scale; a single shared identity makes the `principal` field close to
   decorative.
3. **Revocation: CRL or OCSP?** Who publishes it, and what is the acceptable
   staleness window? This mostly lands on the nginx config, but it determines
   whether a compromised node can be cut off in minutes or in hours.
4. **Is the client on the other end a generic MCP client** (Claude Desktop and
   friends), or Akamai-controlled code? Generic clients cannot do mTLS with a
   private CA — they expect the OAuth profile. This decides whether tier 2 and
   tier 3 must coexist on the same node.

   *Working answer (tentative):* a purpose-built client is expected, either
   here or inside Akamai. If that holds, mTLS is viable end to end and the
   OAuth path becomes a community-facing feature rather than a deployment
   requirement — phase 5 stays in the plan but stops being on anyone's
   critical path. Worth re-confirming before phase 5 is scheduled, because
   the reverse (a generic client appears later) would make it urgent.
5. **Does the 2026-07-28 transport change land on your roadmap**, and when?
   §8 argues the auth layer should be written to survive it either way, but it
   affects sequencing.

---

## 13. Suggested phasing

Each phase is independently shippable and independently green, per the repo's
commit conventions.

| Phase | Contents | Ships value alone? |
|---|---|---|
| 1 | `Principal` type, `scope -> Principal` extraction, `transport.posture` declaration with the unsafe-combination refusal, `backend: none` made explicit, principal and posture recorded in audit + startup record | Yes — "unauthenticated" and "unprotected" become stated choices, and the audit schema is exercised end to end |
| 2 | `header-identity-unverified` backend (§7) | Yes — makes phases 3 and 4 testable over plain HTTP with no PKI |
| 3 | `mtls-proxy-headers` backend, bind-address self-check, reference nginx config in `configs/`, `docs/` deployment note | Yes — closes the Akamai requirement |
| 4 | `policy` block: principal → catalog scoping, default-deny within a present policy | Yes — this is where blast radius actually shrinks |
| 5 | `oauth-bearer` backend via SDK assembly, RFC 9728 metadata route, audience validation | Yes — makes the server usable by generic MCP clients |
| 6 | `static-token`, `mtls-direct` (spike permitting) | Marginal; do last |

Phase 2 is deliberately early despite being the weakest backend: it is the one
that lets the policy engine be developed and tested without standing up a CA
first, which shortens phases 4 and 5.

Phases 1–4 are what Akamai needs. Phase 5 is what the wider ecosystem needs.
They do not block each other.

Every phase carries the usual obligations: a conformance probe per behavioural
change, a `CHANGELOG.md` entry with impact tags, and a migration note for
anything non-obvious to port. Phases 3 and 5 are `[security-boundary]`; phase 4
is `[security-boundary]` and `[behavior-change]`. Phase 2 is
`[security-boundary]` too, despite adding no protection — a backend that
accepts unverified identity is exactly the kind of thing a fork must review
before absorbing.

---

## Sources

- [MCP Authorization specification (draft)](https://modelcontextprotocol.io/specification/draft/basic/authorization)
- [MCP Authorization security considerations](https://modelcontextprotocol.io/specification/draft/basic/authorization/security-considerations)
- [The 2026-07-28 MCP Specification Release Candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- Installed `mcp` 1.28.1: `server/auth/`, `server/sse.py`; installed `uvicorn` 0.51.0
