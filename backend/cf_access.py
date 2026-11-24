"""Fail-closed Cloudflare Access RS256 JWT verification middleware for public tunnel hosts."""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_JWKS_TTL_SECONDS = 600.0
_JWKS_REFRESH_COOLDOWN_SECONDS = 15.0
_CLOCK_SKEW_SECONDS = 60


def _b64url_decode(segment: str) -> bytes:
    rem = len(segment) % 4
    if rem:
        segment += "=" * (4 - rem)
    return base64.urlsafe_b64decode(segment.encode("ascii"))


def _rsa_public_key_from_jwk(jwk: dict[str, Any]) -> rsa.RSAPublicKey | None:
    if jwk.get("kty") != "RSA" or not jwk.get("n") or not jwk.get("e"):
        return None
    n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    return rsa.RSAPublicNumbers(e=e, n=n).public_key()


def _parse_cookie(header_val: str, name: str) -> str | None:
    for part in header_val.split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip() == name and v.strip():
            return v.strip()
    return None


class CloudflareAccessVerifier:
    """Caches team JWKS and verifies RS256 Cloudflare Access assertions."""

    def __init__(self, team_domain: str, *, audience: str | None = None, jwks_fetcher=None) -> None:
        self.team_domain = team_domain.rstrip("/")
        self.certs_url = f"{self.team_domain}/cdn-cgi/access/certs"
        self.audience = audience.strip() if audience else None
        self._jwks_fetcher = jwks_fetcher
        self._lock = threading.Lock()
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched_at: float = 0.0

    def _fetch_jwks(self) -> dict[str, rsa.RSAPublicKey]:
        if self._jwks_fetcher is not None:
            payload = self._jwks_fetcher()
        else:
            req = urllib.request.Request(
                self.certs_url,
                headers={"User-Agent": "t3-trading-desk-cf-access/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        out: dict[str, rsa.RSAPublicKey] = {}
        for jwk in payload.get("keys", []):
            kid = jwk.get("kid")
            pub = _rsa_public_key_from_jwk(jwk)
            if kid and pub is not None:
                out[str(kid)] = pub
        return out

    def _get_key(self, kid: str) -> rsa.RSAPublicKey | None:
        now = time.monotonic()
        with self._lock:
            stale = (now - self._fetched_at) > _JWKS_TTL_SECONDS
            can_refresh = (now - self._fetched_at) > _JWKS_REFRESH_COOLDOWN_SECONDS
            if not stale and kid in self._keys:
                return self._keys[kid]
            if not stale and not can_refresh:
                return self._keys.get(kid)
        try:
            fresh = self._fetch_jwks()
        except Exception:
            with self._lock:
                return self._keys.get(kid)
        with self._lock:
            self._keys = fresh
            self._fetched_at = time.monotonic()
            return self._keys.get(kid)

    def verify(self, token: str) -> tuple[bool, str]:
        parts = token.strip().split(".")
        if len(parts) != 3:
            return False, "malformed_jwt"
        hdr_b64, pay_b64, sig_b64 = parts
        try:
            header = json.loads(_b64url_decode(hdr_b64).decode("utf-8"))
            claims = json.loads(_b64url_decode(pay_b64).decode("utf-8"))
            signature = _b64url_decode(sig_b64)
        except Exception:
            return False, "invalid_jwt_encoding"

        if header.get("alg") != "RS256" or not header.get("kid"):
            return False, "unsupported_alg_or_kid"

        pub = self._get_key(str(header["kid"]))
        if pub is None:
            return False, "unknown_kid"

        signing_input = f"{hdr_b64}.{pay_b64}".encode("ascii")
        try:
            pub.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return False, "invalid_signature"

        now = int(time.time())
        iss = str(claims.get("iss") or "").rstrip("/")
        if iss != self.team_domain:
            return False, "issuer_mismatch"

        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or now > int(exp) + _CLOCK_SKEW_SECONDS:
            return False, "token_expired"

        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and now + _CLOCK_SKEW_SECONDS < int(nbf):
            return False, "token_not_yet_valid"

        if self.audience:
            aud = claims.get("aud")
            aud_list = aud if isinstance(aud, list) else ([aud] if aud else [])
            if self.audience not in aud_list:
                return False, "audience_mismatch"

        return True, "ok"


_LOCKED_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>T3 Trading Desk · 等待 Cloudflare Access 鉴权</title>
  <style>
    body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #161514; color: #ece6dc; display: grid; place-items: center; min-height: 100dvh; padding: 24px; }
    main { max-width: 560px; background: #1f1d1b; border: 1px solid #35312c; border-radius: 14px; padding: 28px; line-height: 1.65; }
    h1 { margin: 0 0 10px; font-size: 1.2rem; color: #f6efe4; }
    code { background: #2b2824; padding: 2px 6px; border-radius: 6px; font-size: 0.92em; }
    ol { padding-left: 20px; color: #cfc7bc; }
    p { color: #b9b0a4; }
  </style>
</head>
<body>
  <main>
    <h1>访问已拦截：缺少有效的 Cloudflare Access 身份凭据</h1>
    <p>为保护持仓账本与私有研报数据，公网入口默认 fail-closed，未通过 Cloudflare Access 校验的请求全部拒绝。</p>
    <ol>
      <li>打开 Cloudflare Zero Trust 控制台 → <code>Access</code> → <code>Applications</code></li>
      <li>新建 Self-hosted 应用，域名填写 <code>desk.xihaozhan.trade</code></li>
      <li>绑定你现有的邮箱 Allow 策略并保存，然后刷新本页即可登录使用</li>
    </ol>
  </main>
</body>
</html>
"""


class CloudflareAccessMiddleware:
    """Enforce Cloudflare Access JWT on public tunnel hosts; local/Tailscale passes through."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        team_domain: str,
        protected_hosts: set[str],
        audience: str | None = None,
        verifier: CloudflareAccessVerifier | None = None,
    ) -> None:
        self.app = app
        self.protected_hosts = {h.strip().lower() for h in protected_hosts if h.strip()}
        self.verifier = verifier or CloudflareAccessVerifier(team_domain, audience=audience)

    def _requires_cf_access(self, headers: dict[bytes, bytes]) -> bool:
        if b"cf-ray" in headers or b"cf-connecting-ip" in headers:
            return True
        host = headers.get(b"host", b"").decode("latin-1").split(":", 1)[0].strip().lower()
        return host in self.protected_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if not self._requires_cf_access(headers):
            await self.app(scope, receive, send)
            return

        token = headers.get(b"cf-access-jwt-assertion", b"").decode("latin-1").strip()
        if not token:
            cookie_hdr = headers.get(b"cookie", b"").decode("latin-1")
            token = _parse_cookie(cookie_hdr, "CF_Authorization") or ""

        if token:
            ok, reason = self.verifier.verify(token)
        else:
            ok, reason = False, "missing_cf_access_jwt"

        if ok:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith("/api/"):
            resp: Any = JSONResponse(
                status_code=403,
                content={"detail": f"Cloudflare Access authentication required ({reason})"},
            )
        else:
            resp = HTMLResponse(status_code=403, content=_LOCKED_HTML)
        await resp(scope, receive, send)
