"""Key handling for the Deep tier.

Layout of ``deep_keys.json``::

    public_key           X25519 public key (base64). Anyone holding it can seal
                         a capture into the inbox; nobody can open one with it.
    salt, ops, mem       Argon2id parameters for the passphrase.
    wrapped_private_key  SecretBox(Argon2id(passphrase)) of the X25519 private key.

The private key is only ever held in memory after an unlock. The Deep DB key is
derived from it, so the passphrase (via Argon2id) is the only way in.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import nacl.encoding
import nacl.exceptions
import nacl.hash
import nacl.pwhash
import nacl.secret
import nacl.utils
from nacl.public import PrivateKey, PublicKey, SealedBox

KDF_LEVELS = {
    "interactive": (nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE, nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE),
    "moderate": (nacl.pwhash.argon2id.OPSLIMIT_MODERATE, nacl.pwhash.argon2id.MEMLIMIT_MODERATE),
    "sensitive": (nacl.pwhash.argon2id.OPSLIMIT_SENSITIVE, nacl.pwhash.argon2id.MEMLIMIT_SENSITIVE),
}


class BadPassphrase(Exception):
    pass


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _kek(passphrase: str, salt: bytes, ops: int, mem: int) -> bytes:
    return nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE, passphrase.encode("utf-8"), salt, opslimit=ops, memlimit=mem
    )


def db_key_from_private(private_key: PrivateKey) -> bytes:
    return nacl.hash.blake2b(
        bytes(private_key), digest_size=32, person=b"barometer-db", encoder=nacl.encoding.RawEncoder
    )


@dataclass
class DeepKeys:
    public_key: PublicKey
    salt: bytes
    ops: int
    mem: int
    wrapped_private_key: bytes

    @classmethod
    def create(cls, passphrase: str, strength: str = "moderate") -> tuple["DeepKeys", PrivateKey]:
        if len(passphrase) < 8:
            raise ValueError("Deep passphrase must be at least 8 characters")
        ops, mem = KDF_LEVELS[strength]
        private = PrivateKey.generate()
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        wrapped = nacl.secret.SecretBox(_kek(passphrase, salt, ops, mem)).encrypt(bytes(private))
        return cls(private.public_key, salt, ops, mem, bytes(wrapped)), private

    def unwrap(self, passphrase: str) -> PrivateKey:
        try:
            raw = nacl.secret.SecretBox(_kek(passphrase, self.salt, self.ops, self.mem)).decrypt(
                self.wrapped_private_key
            )
        except nacl.exceptions.CryptoError as exc:
            raise BadPassphrase() from exc
        return PrivateKey(raw)

    def rewrap(self, private: PrivateKey, new_passphrase: str, strength: str = "moderate") -> "DeepKeys":
        ops, mem = KDF_LEVELS[strength]
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        wrapped = nacl.secret.SecretBox(_kek(new_passphrase, salt, ops, mem)).encrypt(bytes(private))
        return DeepKeys(self.public_key, salt, ops, mem, bytes(wrapped))

    def seal(self, payload: dict) -> bytes:
        return SealedBox(self.public_key).encrypt(json.dumps(payload).encode("utf-8"))

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "public_key": b64e(bytes(self.public_key)),
                "salt": b64e(self.salt),
                "ops": self.ops,
                "mem": self.mem,
                "wrapped_private_key": b64e(self.wrapped_private_key),
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "DeepKeys":
        raw = json.loads(text)
        return cls(
            PublicKey(b64d(raw["public_key"])),
            b64d(raw["salt"]),
            int(raw["ops"]),
            int(raw["mem"]),
            b64d(raw["wrapped_private_key"]),
        )

    def save(self, path: Path) -> None:
        write_atomic(path, self.to_json().encode("utf-8"), mode=0o600)

    @classmethod
    def load(cls, path: Path) -> "DeepKeys":
        return cls.from_json(path.read_text())


def open_sealed(private: PrivateKey, blob: bytes) -> dict:
    return json.loads(SealedBox(private).decrypt(blob).decode("utf-8"))


def secret_encrypt(key: bytes, data: bytes) -> bytes:
    return bytes(nacl.secret.SecretBox(key).encrypt(data))


def secret_decrypt(key: bytes, blob: bytes) -> bytes:
    return nacl.secret.SecretBox(key).decrypt(blob)


def hash_password(password: str) -> str:
    return nacl.pwhash.argon2id.str(password.encode("utf-8")).decode("ascii")


def verify_password(stored: str, password: str) -> bool:
    try:
        return nacl.pwhash.verify(stored.encode("ascii"), password.encode("utf-8"))
    except nacl.exceptions.InvalidkeyError:
        return False


def random_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(nacl.utils.random(nbytes)).decode("ascii").rstrip("=")


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write-then-rename so a power cut never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
