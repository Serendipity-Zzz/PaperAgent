from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class LocalSessionTokens:
    def __init__(self, secret: bytes | None = None, *, ttl_seconds: int = 3600) -> None:
        self.secret = secret or secrets.token_bytes(32)
        self.ttl_seconds = ttl_seconds

    def issue(self, *, now: int | None = None) -> str:
        issued = now if now is not None else int(time.time())
        payload = json.dumps(
            {"iat": issued, "exp": issued + self.ttl_seconds}, separators=(",", ":")
        )
        encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str, *, now: int | None = None) -> bool:
        try:
            payload_part, signature_part = token.split(".", 1)
            encoded = payload_part.encode()
            expected = hmac.new(self.secret, encoded, hashlib.sha256).digest()
            signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
            if not hmac.compare_digest(signature, expected):
                return False
            payload = json.loads(
                base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
            )
            current = now if now is not None else int(time.time())
            return int(payload["iat"]) <= current <= int(payload["exp"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
