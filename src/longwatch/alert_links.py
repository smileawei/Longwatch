from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable


class AlertLinkSigner:
    """Issue random, signed alert tokens that expire without server-side storage."""

    def __init__(
        self,
        secret: str,
        ttl_seconds: int = 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ):
        if not secret:
            raise ValueError("告警链接签名密钥不能为空")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def issue(self, symbol: str) -> str:
        expires_at = int(self._clock()) + self._ttl_seconds
        nonce = secrets.token_urlsafe(16)
        payload = self._encode(f"{symbol}|{expires_at}|{nonce}".encode("utf-8"))
        signature = self._encode(
            hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{payload}.{signature}"

    def verify(self, token: str, symbol: str) -> bool:
        if not token or len(token) > 512:
            return False
        try:
            payload, supplied_signature = token.split(".", 1)
            expected_signature = self._encode(
                hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return False
            token_symbol, expires_at, _nonce = self._decode(payload).decode("utf-8").split("|", 2)
            return token_symbol == symbol and int(expires_at) >= int(self._clock())
        except (UnicodeDecodeError, ValueError):
            return False
