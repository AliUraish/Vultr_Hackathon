"""Signed fleet policies (Ed25519).

The control plane signs every policy version it approves; sim nodes hold only the public key and
refuse to run a policy whose signature does not verify. A compromised network path or a stolen
node token can then no longer push rules to the fleet: only the holder of the signing key can.

Keys are PEM files (infra/deploy.sh generates them); without keys configured, both sides run in
unsigned mode, which the UI and /health report.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:  # pragma: no cover - only in environments without the package
    Ed25519PrivateKey = Ed25519PublicKey = None  # type: ignore[assignment,misc]


class SignatureError(ValueError):
    pass


def message(version: int, rules: list[str]) -> bytes:
    """What gets signed: the version number and the exact normalized rules, canonically encoded."""
    return json.dumps({"version": int(version), "rules": list(rules)}, sort_keys=True, separators=(",", ":")).encode()


def _raw_public(pub: "Ed25519PublicKey") -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(pub: "Ed25519PublicKey") -> str:
    return hashlib.sha256(_raw_public(pub)).hexdigest()[:16]


class Signer:
    def __init__(self, private_pem: bytes) -> None:
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SignatureError("policy signing key must be Ed25519")
        self._key = key
        self.public = key.public_key()
        self.key_id = key_id(self.public)

    @classmethod
    def from_file(cls, path: str | None) -> "Signer | None":
        return cls(Path(path).read_bytes()) if path and Path(path).exists() else None

    @classmethod
    def generate(cls) -> "Signer":
        pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption())
        return cls(pem)

    def public_pem(self) -> bytes:
        return self.public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, version: int, rules: list[str]) -> str:
        return base64.b64encode(self._key.sign(message(version, rules))).decode()


class Verifier:
    def __init__(self, public_pem: bytes) -> None:
        key = serialization.load_pem_public_key(public_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise SignatureError("policy public key must be Ed25519")
        self._key = key
        self.key_id = key_id(key)

    @classmethod
    def from_file(cls, path: str | None) -> "Verifier | None":
        return cls(Path(path).read_bytes()) if path and Path(path).exists() else None

    def verify(self, version: int, rules: list[str], signature: str | None) -> None:
        if not signature:
            raise SignatureError("policy is not signed")
        try:
            self._key.verify(base64.b64decode(signature), message(version, rules))
        except (InvalidSignature, ValueError) as exc:
            raise SignatureError("policy signature does not verify") from exc


__all__ = ["SignatureError", "Signer", "Verifier", "key_id", "message"]
