"""The Deep store: the private tier.

At rest it is a single SecretBox-encrypted SQLite image (``deep.db.enc``). On unlock
the image is decrypted into an in-memory SQLite database; every committed
transaction re-encrypts and atomically rewrites the file. While locked, captures are
sealed to the Deep public key and parked in the Almanac's ``deep_inbox``.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from nacl.public import PrivateKey

from .config import Settings
from .crypto import BadPassphrase, DeepKeys, db_key_from_private, open_sealed, secret_decrypt, secret_encrypt, write_atomic
from .db import DEEP_SCHEMA, Store, connect_memory


class DeepLocked(Exception):
    pass


class DeepVault:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.keys: DeepKeys | None = (
            DeepKeys.load(settings.deep_keys_path) if settings.deep_keys_path.exists() else None
        )
        self._private: PrivateKey | None = None
        self._db_key: bytes | None = None
        self._store: Store | None = None
        self.lock = threading.RLock()
        self.last_activity = 0.0

    # -- lifecycle --------------------------------------------------------------
    @property
    def initialised(self) -> bool:
        return self.keys is not None

    @property
    def is_open(self) -> bool:
        return self._store is not None

    def initialise(self, passphrase: str) -> None:
        if self.initialised:
            raise RuntimeError("Deep store already initialised")
        keys, private = DeepKeys.create(passphrase, self.settings.kdf_strength)
        keys.save(self.settings.deep_keys_path)
        self.keys = keys
        self._open_with(private)

    def verify(self, passphrase: str) -> PrivateKey:
        if not self.keys:
            raise RuntimeError("Deep store not initialised; run `barometer init`")
        return self.keys.unwrap(passphrase)

    def unlock(self, passphrase: str) -> None:
        with self.lock:
            private = self.verify(passphrase)
            if not self.is_open:
                self._open_with(private)
            self.touch()

    def _open_with(self, private: PrivateKey) -> None:
        db_key = db_key_from_private(private)
        conn = connect_memory()
        path = self.settings.deep_path
        if path.exists():
            conn.deserialize(secret_decrypt(db_key, path.read_bytes()))
        store = Store(conn)
        store.migrate(DEEP_SCHEMA)
        self._private, self._db_key, self._store = private, db_key, store
        self.persist()
        self.touch()

    def lock_now(self) -> None:
        with self.lock:
            if self._store is not None:
                self._store.conn.close()
            self._store = None
            self._private = None
            self._db_key = None

    def change_passphrase(self, old: str, new: str) -> None:
        private = self.verify(old)
        assert self.keys is not None
        self.keys = self.keys.rewrap(private, new, self.settings.kdf_strength)
        self.keys.save(self.settings.deep_keys_path)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_activity

    # -- data access ------------------------------------------------------------
    @property
    def store(self) -> Store:
        store = self._store
        if store is None:
            raise DeepLocked()
        return store

    @contextmanager
    def tx(self):
        with self.lock:
            store = self.store
            with store.tx() as conn:
                yield conn
            self.persist()

    def persist(self) -> None:
        with self.lock:
            if self._store is None or self._db_key is None:
                return
            with self._store.lock:
                image = self._store.conn.serialize()
            write_atomic(self.settings.deep_path, secret_encrypt(self._db_key, image))

    def vacuum(self) -> None:
        with self.lock:
            self.store.conn.execute("VACUUM")
            self.persist()

    # -- sealing ----------------------------------------------------------------
    def seal(self, payload: dict) -> bytes:
        if not self.keys:
            raise RuntimeError("Deep store not initialised")
        return self.keys.seal(payload)

    def open_sealed(self, blob: bytes) -> dict:
        if self._private is None:
            raise DeepLocked()
        return open_sealed(self._private, blob)


__all__ = ["DeepVault", "DeepLocked", "BadPassphrase"]
