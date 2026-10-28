"""Encrypted backups. The backup key pair is made off the Pi; only its public half lives here.

A backup is a tar of the Almanac (a consistent SQLite snapshot), the already-encrypted
Deep image, the wrapped Deep key file and any sealed inbox. The whole tar is sealed to
the backup public key, so the Pi can write backups it cannot read.
"""

from __future__ import annotations

import io
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from nacl.public import PrivateKey, PublicKey, SealedBox

from .config import Settings
from .crypto import b64d, b64e, write_atomic


def keygen() -> tuple[str, str]:
    key = PrivateKey.generate()
    return b64e(bytes(key)), b64e(bytes(key.public_key))


def _snapshot_almanac(settings: Settings, dest: Path) -> None:
    if settings.almanac_key_file:
        # SQLCipher: copy the encrypted file as-is (WAL checkpointed first by the caller's connection).
        dest.write_bytes(settings.almanac_path.read_bytes())
        return
    src = sqlite3.connect(str(settings.almanac_path))
    try:
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()


def make_backup(settings: Settings) -> Path:
    if not settings.backup_dir or not settings.backup_public_key:
        raise RuntimeError("Set BAROMETER_BACKUP_DIR and BAROMETER_BACKUP_PUBLIC_KEY first")
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
        snap = Path(tmp) / "almanac.db"
        _snapshot_almanac(settings, snap)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="almanac.db")
            for path in (settings.deep_path, settings.deep_keys_path):
                if path.exists():
                    tar.add(path, arcname=path.name)
    sealed = SealedBox(PublicKey(b64d(settings.backup_public_key))).encrypt(buf.getvalue())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = settings.backup_dir / f"barometer-{stamp}.bak"
    write_atomic(out, bytes(sealed))
    backups = sorted(settings.backup_dir.glob("barometer-*.bak"))
    for old in backups[: max(0, len(backups) - settings.backup_keep)]:
        old.unlink()
    return out


def restore(backup: Path, private_key_b64: str, dest: Path) -> list[str]:
    data = SealedBox(PrivateKey(b64d(private_key_b64))).decrypt(backup.read_bytes())
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                raise RuntimeError(f"Unexpected entry in backup: {member.name}")
            fh = tar.extractfile(member)
            assert fh is not None
            write_atomic(dest / member.name, fh.read())
            names.append(member.name)
    return names


def verify(backup: Path, private_key_b64: str, deep_passphrase: str | None = None) -> dict:
    """Restore into a temp dir and open everything. Run this off the Pi, regularly."""
    from .config import load_settings
    from .deep import DeepVault

    with tempfile.TemporaryDirectory() as tmp:
        files = restore(backup, private_key_b64, Path(tmp))
        conn = sqlite3.connect(str(Path(tmp) / "almanac.db"))
        people = conn.execute("SELECT count(*) FROM people").fetchone()[0]
        conn.close()
        report = {"files": files, "people": people}
        if deep_passphrase:
            vault = DeepVault(load_settings(tmp))
            vault.unlock(deep_passphrase)
            report["notes"] = vault.store.scalar("SELECT count(*) FROM notes", (), 0)
            vault.lock_now()
        return report
