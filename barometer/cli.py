"""Command line: setup, serving, backups, the golden set and the full wipe."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config import load_settings


def _ask_new(prompt: str, minimum: int) -> str:
    while True:
        first = getpass.getpass(prompt)
        if len(first) < minimum:
            print(f"  At least {minimum} characters, please.")
            continue
        if getpass.getpass("  Again: ") != first:
            print("  Those didn't match.")
            continue
        return first


def cmd_init(args) -> int:
    from .almanac import Almanac
    from .auth import add_user
    from .deep import DeepVault

    settings = load_settings(args.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    almanac = Almanac(settings)
    almanac.self_id()
    if almanac.scalar("SELECT count(*) FROM users WHERE role = 'owner'", (), 0):
        print("An owner already exists. Use `barometer add-user` for Almanac logins.")
    else:
        username = args.username or input("Owner username: ").strip()
        add_user(almanac, username, _ask_new("Login passphrase (10+ chars): ", 10), "owner")
        print(f"Created owner login '{username}'.")
    vault = DeepVault(settings)
    if vault.initialised:
        print("Private (Deep) store already set up.")
    else:
        print("\nNow the Private passphrase. It is never stored; lose it and Private is gone for good.")
        vault.initialise(_ask_new("Private passphrase (12+ chars): ", 12))
        vault.lock_now()
        print("Private store created and locked.")
    print(f"\nData directory: {settings.data_dir}")
    return 0


def cmd_add_user(args) -> int:
    from .almanac import Almanac
    from .auth import add_user

    settings = load_settings(args.data_dir)
    add_user(Almanac(settings), args.username, _ask_new("Login passphrase (10+ chars): ", 10), args.role)
    print(f"Added {args.role} login '{args.username}'.")
    return 0


def cmd_passwd(args) -> int:
    from .almanac import Almanac
    from .auth import set_password

    settings = load_settings(args.data_dir)
    set_password(Almanac(settings), args.username, _ask_new("New login passphrase: ", 10))
    print("Updated.")
    return 0


def cmd_deep_passphrase(args) -> int:
    from .deep import DeepVault

    vault = DeepVault(load_settings(args.data_dir))
    old = getpass.getpass("Current Private passphrase: ")
    vault.change_passphrase(old, _ask_new("New Private passphrase (12+ chars): ", 12))
    print("Private passphrase changed.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    settings = load_settings(args.data_dir)
    if args.data_dir:
        os.environ["BAROMETER_DATA_DIR"] = str(settings.data_dir)
    uvicorn.run(
        "barometer.app:make_default_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


def cmd_digest(args) -> int:
    from .almanac import Almanac
    from .relationships import send_weekly_push, today_view

    settings = load_settings(args.data_dir)
    almanac = Almanac(settings)
    today = datetime.now(settings.tz).date()
    view = today_view(almanac, today)
    print(f"Week of {view['week_start']}: {len(view['dates'])} dates, {len(view['follow_ups'])} follow-ups, {len(view['nudges'])} nudges")
    if args.push:
        print("Pushed." if send_weekly_push(almanac, settings.ntfy_url, today) else "Not pushed (already sent, or no NTFY_URL).")
    return 0


def cmd_backup(args) -> int:
    from .backup import make_backup

    print(make_backup(load_settings(args.data_dir)))
    return 0


def cmd_backup_keygen(_args) -> int:
    from .backup import keygen

    private, public = keygen()
    print("Keep the PRIVATE key off the Pi (password manager, paper). Without it, backups can't be read.\n")
    print(f"BACKUP PRIVATE KEY: {private}")
    print(f"\nPut this line in the Pi's .env:\nBAROMETER_BACKUP_PUBLIC_KEY={public}")
    return 0


def cmd_restore(args) -> int:
    from .backup import restore

    key = getpass.getpass("Backup private key: ").strip()
    dest = Path(args.to)
    if dest.exists() and any(dest.iterdir()) and not args.force:
        print(f"{dest} is not empty; use --force to overwrite.")
        return 1
    for name in restore(Path(args.backup), key, dest):
        print(f"restored {dest / name}")
    return 0


def cmd_restore_test(args) -> int:
    from .backup import verify

    key = getpass.getpass("Backup private key: ").strip()
    passphrase = getpass.getpass("Private passphrase (blank to skip opening Private): ") or None
    report = verify(Path(args.backup), key, passphrase)
    print(json.dumps(report, indent=2))
    return 0


def cmd_golden(args) -> int:
    from .config import Settings
    from .golden import corrections_as_cases, load_cases, run_case
    from .llm import make_llm

    settings = load_settings(args.data_dir)
    cases = load_cases(Path(args.cases))
    if args.include_corrections:
        from .deep import DeepVault

        vault = DeepVault(settings)
        vault.unlock(getpass.getpass("Private passphrase (to read your corrections): "))
        cases += corrections_as_cases(vault)
        vault.lock_now()

    def factory():
        s: Settings = load_settings(args.data_dir)
        if args.llm:
            s.llm = args.llm
        return make_llm(s)

    results = []
    for case in cases:
        r = run_case(case, factory)
        results.append(r)
        mark = "pass" if r.passed else "FAIL"
        print(f"{mark}  {r.id}" + ("" if r.passed else "\n      " + "\n      ".join(r.problems)))
    passed = sum(r.passed for r in results)
    share = passed / len(results) if results else 0
    print(f"\n{passed}/{len(results)} needed no correction ({share:.0%}). Target: 90%.")
    return 0 if share >= 0.9 else 1


def cmd_wipe(args) -> int:
    settings = load_settings(args.data_dir)
    print(f"This permanently deletes everything in {settings.data_dir}.")
    if input("Type WIPE to continue: ").strip() != "WIPE":
        print("Cancelled.")
        return 1
    for path in sorted(settings.data_dir.rglob("*"), reverse=True):
        if path.is_file() and path.name != ".env":
            try:
                size = path.stat().st_size
                with open(path, "r+b") as fh:
                    fh.write(os.urandom(min(size, 64 * 1024 * 1024)))
            except OSError:
                pass
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    print("Wiped. (Backups elsewhere are untouched; delete them separately.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="barometer", description="Barometer: read the pressure before the weather changes.")
    parser.add_argument("--data-dir", help="Data directory (default: $BAROMETER_DATA_DIR or ./data)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Create the owner login and the Private store")
    p.add_argument("--username")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add-user", help="Add a login")
    p.add_argument("username")
    p.add_argument("--role", choices=["almanac", "owner"], default="almanac")
    p.set_defaults(fn=cmd_add_user)

    p = sub.add_parser("passwd", help="Change a login passphrase")
    p.add_argument("username")
    p.set_defaults(fn=cmd_passwd)

    p = sub.add_parser("deep-passphrase", help="Change the Private passphrase")
    p.set_defaults(fn=cmd_deep_passphrase)

    p = sub.add_parser("serve", help="Run the web app")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("digest", help="Build this week's digest (and push it with --push)")
    p.add_argument("--push", action="store_true")
    p.set_defaults(fn=cmd_digest)

    sub.add_parser("backup", help="Write an encrypted backup now").set_defaults(fn=cmd_backup)
    sub.add_parser("backup-keygen", help="Make a backup key pair (run this off the Pi)").set_defaults(fn=cmd_backup_keygen)

    p = sub.add_parser("restore", help="Restore a backup into a directory")
    p.add_argument("backup")
    p.add_argument("--to", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("restore-test", help="Check a backup decrypts and opens")
    p.add_argument("backup")
    p.set_defaults(fn=cmd_restore_test)

    p = sub.add_parser("golden", help="Run the golden extraction set")
    p.add_argument("--cases", default=str(Path(__file__).resolve().parent.parent / "golden" / "cases.jsonl"))
    p.add_argument("--include-corrections", action="store_true")
    p.add_argument("--llm", choices=["claude", "fake"])
    p.set_defaults(fn=cmd_golden)

    sub.add_parser("wipe", help="Delete all data, irreversibly").set_defaults(fn=cmd_wipe)

    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["BAROMETER_DATA_DIR"] = args.data_dir
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
