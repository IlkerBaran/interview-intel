"""
proxy.py

Client-IP resolution for reverse-proxy deployments.

Rate limits key on the client IP, so using the proxy's socket address would put
multiple users in the same rate-limit bucket.

Werkzeug's ProxyFix normally resolves X-Forwarded-For with `x_for=N`, counting
from the right. That requires a stable proxy-chain length. Railway's chain
length varies by routing path, so no fixed N is correct on every request
(ADR-0012). A wrong count fails silently: it can leave REMOTE_ADDR as the
internal peer or select an intermediary instead of the real client.

Railway's stable value is the LEFTMOST X-Forwarded-For entry. Its ingress
overwrites client-supplied XFF and writes the real client address first, so this
middleware uses position 0 when CLIENT_IP_SOURCE=xff-leftmost.

This trust is safe ONLY when the ingress overwrites incoming X-Forwarded-For.
Otherwise the leftmost value is client-controlled and spoofable. For that
reason the middleware is opt-in; local development, Docker, and other direct
deployments use the socket peer and ignore forwarded headers.

X-Real-IP is intentionally not used. On Railway's CDN path it may contain the
edge address rather than the real client IP. See ADR-0012 for details.
"""

from ipaddress import ip_address


class ForwardedForLeftmost:
    """
    Rewrite REMOTE_ADDR from the FIRST value of X-Forwarded-For.

    Install this ONLY where the ingress is known to replace a client-supplied
    X-Forwarded-For rather than append to it. See the module docstring.

    Rewriting the WSGI environ, rather than resolving the address at each call
    site, is deliberate: `request.remote_addr` is what Flask-Limiter's
    `get_remote_address` reads, what the two fallback key functions in
    extensions.py read, and what any future security-sensitive code will reach
    for by reflex. Fixing it once in the environ leaves no second definition of
    "the client" to drift out of sync with this one.
    """

    #: Where the pre-rewrite peer address is kept, mirroring ProxyFix's
    #: `werkzeug.proxy_fix.orig` convention so the real socket peer is still
    #: recoverable for debugging.
    ORIG_KEY = "app.proxy.orig_remote_addr"

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        client = self._leftmost(environ.get("HTTP_X_FORWARDED_FOR"))

        if client is not None:
            environ[self.ORIG_KEY] = environ.get("REMOTE_ADDR")
            environ["REMOTE_ADDR"] = client

        return self.app(environ, start_response)

    @staticmethod
    def _leftmost(header):
        """
        The first X-Forwarded-For value, or None if there isn't a usable one.

        Returning None leaves REMOTE_ADDR as the socket peer. That is the safe
        direction to fail: an unusable header collapses the request into the
        shared peer bucket, which is more restrictive, rather than keying a
        limit on an arbitrary attacker-chosen string.

        The value is parsed as an IP address rather than passed through as
        text, for two reasons. X-Forwarded-For is allowed to carry things that
        are not bare addresses — `unknown`, obfuscated identifiers, `host:port`
        pairs — and none of those should become a rate-limit key. And parsing
        normalises the address, so `2001:DB8::1` and `2001:db8::1` cannot
        occupy two separate buckets for the same client.
        """
        if not header:
            return None

        candidate = header.split(",", 1)[0].strip()

        try:
            return str(ip_address(candidate))
        except ValueError:
            return None
