"""Unit tests for fail-closed Cloudflare Access RS256 verification."""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backend.cf_access import CloudflareAccessMiddleware, CloudflareAccessVerifier


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_rsa_pair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_nums = priv.public_key().public_numbers()
    n_bytes = pub_nums.n.to_bytes((pub_nums.n.bit_length() + 7) // 8, "big")
    e_bytes = pub_nums.e.to_bytes((pub_nums.e.bit_length() + 7) // 8, "big")
    jwk = {"kty": "RSA", "kid": "test-kid-1", "alg": "RS256", "n": _b64u(n_bytes), "e": _b64u(e_bytes)}
    return priv, {"keys": [jwk]}


def _sign_jwt(priv, payload: dict, kid: str = "test-kid-1") -> str:
    hdr = _b64u(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode("utf-8"))
    pay = _b64u(json.dumps(payload).encode("utf-8"))
    sig = priv.sign(f"{hdr}.{pay}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{hdr}.{pay}.{_b64u(sig)}"


def _build_client(jwks: dict):
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return {"page": "home"}

    verifier = CloudflareAccessVerifier(
        "https://xihaozhan.cloudflareaccess.com",
        jwks_fetcher=lambda: jwks,
    )
    app.add_middleware(
        CloudflareAccessMiddleware,
        team_domain="https://xihaozhan.cloudflareaccess.com",
        protected_hosts={"desk.xihaozhan.trade"},
        verifier=verifier,
    )
    return TestClient(app)


def test_local_host_bypasses_cf_access():
    _, jwks = _make_rsa_pair()
    client = _build_client(jwks)
    assert client.get("/api/health").status_code == 200


def test_public_host_without_jwt_blocked_403():
    _, jwks = _make_rsa_pair()
    client = _build_client(jwks)
    r_api = client.get("/api/health", headers={"Host": "desk.xihaozhan.trade"})
    assert r_api.status_code == 403
    assert "missing_cf_access_jwt" in r_api.json()["detail"]

    r_html = client.get("/", headers={"CF-Ray": "abc123-SIN"})
    assert r_html.status_code == 403
    assert "Cloudflare Access" in r_html.text


def test_forged_and_expired_jwt_rejected():
    priv, jwks = _make_rsa_pair()
    other_priv, _ = _make_rsa_pair()
    client = _build_client(jwks)

    forged = _sign_jwt(other_priv, {"iss": "https://xihaozhan.cloudflareaccess.com", "exp": int(time.time()) + 300})
    r1 = client.get("/api/health", headers={"Host": "desk.xihaozhan.trade", "Cf-Access-Jwt-Assertion": forged})
    assert r1.status_code == 403
    assert "invalid_signature" in r1.json()["detail"]

    expired = _sign_jwt(priv, {"iss": "https://xihaozhan.cloudflareaccess.com", "exp": int(time.time()) - 300})
    r2 = client.get("/api/health", headers={"Host": "desk.xihaozhan.trade", "Cf-Access-Jwt-Assertion": expired})
    assert r2.status_code == 403
    assert "token_expired" in r2.json()["detail"]


def test_valid_rs256_jwt_allowed_via_header_or_cookie():
    priv, jwks = _make_rsa_pair()
    client = _build_client(jwks)
    good = _sign_jwt(priv, {"iss": "https://xihaozhan.cloudflareaccess.com", "exp": int(time.time()) + 600})

    r1 = client.get("/api/health", headers={"Host": "desk.xihaozhan.trade", "Cf-Access-Jwt-Assertion": good})
    assert r1.status_code == 200

    r2 = client.get("/api/health", headers={"CF-Ray": "xyz-SIN", "Cookie": f"CF_Authorization={good}"})
    assert r2.status_code == 200
