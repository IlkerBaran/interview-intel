"""
proxy.py

Client-IP resolution for the Cloudflare-fronted deployment.

Rate limits key on the client IP, so using the proxy's socket address would put
multiple users in the same rate-limit bucket.

The public ingress is Cloudflare, in front of Railway, in front of this app.
Measured live through https://interview-intel.com (ADR-0012):

* `CF-Connecting-IP` carried the real client address on every request.
* The leftmost `X-Forwarded-For` value did NOT — Railway sees Cloudflare as its
  connecting client, so position 0 is a Cloudflare edge, not the user. The
  `xff-leftmost` mode that was correct on direct Railway is gone for that
  reason: through Cloudflare it would silently key rate limits on Cloudflare's
  own addresses.
* A client-supplied `CF-Connecting-IP` was rejected by Cloudflare with a 403
  before reaching Railway at all.

So when CLIENT_IP_SOURCE=cf-connecting-ip this middleware reads exactly one
header for the address, `CF-Connecting-IP`, and nothing else. `X-Forwarded-For`
is never consulted in this mode. `X-Real-IP` is never consulted in any mode.

`CF-Connecting-IP` is only meaningful on a request that actually came through
Cloudflare — Cloudflare's own guidance — and the app does not take that on
faith. Cloudflare sets a private header, `X-Interview-Intel-Origin`, to a
shared secret on every request it forwards (a Request Header Transform rule).
The middleware trusts `CF-Connecting-IP` only when that header is present and
matches CF_ORIGIN_SECRET in constant time; otherwise the request is treated as
having reached the app around Cloudflare and REMOTE_ADDR stays the socket
peer. The platform-issued Railway hostname answering 404 is evidence that no
such route exists today, not a guarantee that none ever will, and this check
is what makes the difference not matter.

The residual trust is therefore in the secret, not in the network path: it is
safe while the secret is known to Cloudflare and this app alone. It must never
be committed, and this module never logs it or the value a request presented.
The mode is opt-in and never a default: local development, Docker, and any
direct deployment use the socket peer, ignore forwarded headers, and need no
secret.
"""

import logging
from hmac import compare_digest
from ipaddress import ip_address

logger = logging.getLogger(__name__)


class CfConnectingIp:
    """
    Rewrite REMOTE_ADDR from `CF-Connecting-IP`, on requests that prove they
    came through Cloudflare.

    Install this ONLY where Cloudflare is the public ingress and has been
    configured to set `ORIGIN_HEADER` to the same secret this instance holds.
    See the module docstring.

    Rewriting the WSGI environ, rather than resolving the address at each call
    site, is deliberate: `request.remote_addr` is what Flask-Limiter's
    `get_remote_address` reads, what the two fallback key functions in
    extensions.py read, and what any future security-sensitive code will reach
    for by reflex. Fixing it once in the environ leaves no second definition of
    "the client" to drift out of sync with this one.
    """

    #: The WSGI environ key for `CF-Connecting-IP`. Werkzeug does not know this
    #: header, so it is named here once rather than spelled at the call site.
    HEADER_KEY = "HTTP_CF_CONNECTING_IP"

    #: The header Cloudflare's transform rule sets to the shared secret, and
    #: its WSGI environ key. The name is deliberately app-specific: a generic
    #: one invites a copy-pasted rule from another zone.
    ORIGIN_HEADER = "X-Interview-Intel-Origin"
    ORIGIN_HEADER_KEY = "HTTP_X_INTERVIEW_INTEL_ORIGIN"

    #: Where the pre-rewrite peer address is kept, mirroring ProxyFix's
    #: `werkzeug.proxy_fix.orig` convention so the real socket peer is still
    #: recoverable for debugging.
    ORIG_KEY = "app.proxy.orig_remote_addr"

    def __init__(self, app, origin_secret):
        # Refusing here rather than falling through to "never trust" is the
        # same choice config.py makes for a missing CLIENT_IP_SOURCE: a
        # middleware that can never authenticate a request would put every
        # user in the peer bucket with a healthy log.
        if not origin_secret or not origin_secret.strip():
            raise ValueError(
                "CfConnectingIp needs a non-empty origin secret (CF_ORIGIN_SECRET); "
                "without one there is no way to tell a request that came through "
                "Cloudflare from one that did not"
            )
        self.app = app
        self._secret = origin_secret.strip().encode("utf-8")

    def __call__(self, environ, start_response):
        header = environ.get(self.HEADER_KEY)

        if header is not None and not self._through_cloudflare(environ):
            # The address header arrived without proof of passage through
            # Cloudflare: either the transform rule is missing, or something
            # reached the origin directly. Both are worth an operator's
            # attention and both fail the same restrictive way. The presented
            # values are deliberately not logged — one is attacker-chosen
            # text, the other may be a near-miss of the secret.
            logger.warning(
                "CF-Connecting-IP present without a valid %s header — "
                "using the socket peer %s instead",
                self.ORIGIN_HEADER,
                environ.get("REMOTE_ADDR"),
            )
            return self.app(environ, start_response)

        client = self._client(header)

        if client is not None:
            environ[self.ORIG_KEY] = environ.get("REMOTE_ADDR")
            environ["REMOTE_ADDR"] = client

        return self.app(environ, start_response)

    def _through_cloudflare(self, environ):
        """
        True only if the request carries the origin secret Cloudflare sets.

        compare_digest rather than `==`: a plain comparison returns at the
        first differing byte, and the difference is measurable across enough
        requests to recover the secret one byte at a time. The comparison is
        over bytes so both sides are the same type, which compare_digest
        requires. A missing header is False without any comparison at all.
        """
        presented = environ.get(self.ORIGIN_HEADER_KEY)

        if not presented:
            return False

        return compare_digest(presented.strip().encode("utf-8"), self._secret)

    @staticmethod
    def _client(header):
        """
        The `CF-Connecting-IP` value as a normalised address, or None if there
        isn't a usable one.

        Returning None leaves REMOTE_ADDR as the socket peer. That is the safe
        direction to fail: a missing or unusable header collapses the request
        into the shared peer bucket, which is more restrictive, rather than
        keying a limit on an arbitrary string. A missing header is expected on
        requests that never crossed Cloudflare — the container healthcheck, or
        the Cloudflare posture run locally.

        The value is parsed as an IP address rather than passed through as
        text. Cloudflare writes exactly one bare address here, so anything else
        — a comma-separated list, `host:port`, `unknown`, an empty string — is
        not something Cloudflare produced and must not become a rate-limit key.
        Parsing also normalises the address, so `2001:DB8::1` and `2001:db8::1`
        cannot occupy two separate buckets for the same client.
        """
        if not header:
            return None

        try:
            address = ip_address(header.strip())
        except ValueError:
            return None

        # Python accepts an IPv6 zone ID (`fe80::1%eth0`) and keeps it in the
        # string form. Cloudflare never writes one, and a zone is an interface
        # name, not part of the client's identity — so it is not a bare
        # address by the rule above and is discarded the same way.
        if getattr(address, "scope_id", None):
            return None

        return str(address)
