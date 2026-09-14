"""An intentionally tiny, real SQLite durability probe; no network clients."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import sys


class ProofError(RuntimeError):
    pass


def file_hash(path):
    with path.open("rb") as database:
        return hashlib.file_digest(database, "sha256").hexdigest()


def initial(path):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    nonce = secrets.token_hex(32)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, nonce TEXT NOT NULL)")
        connection.execute("INSERT INTO proof VALUES (1, ?)", (nonce,))
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint != (0, 0, 0):
            raise ProofError("SQLite checkpoint did not finish")
    finally:
        connection.close()
    with path.open("rb") as database:
        os.fsync(database.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"nonce": nonce, "sha256": file_hash(path)}


def recover(path, nonce, sha256):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ProofError("recovery requires an existing regular database")
    if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in (nonce, sha256)):
        raise ProofError("external expected nonce and SHA256 are required")
    for suffix in ("-wal", "-journal"):
        journal = Path(str(path) + suffix)
        if journal.exists() and journal.stat().st_size:
            raise ProofError("database hash cannot be verified with a pending journal")
    if file_hash(path) != sha256:
        raise ProofError("database hash differs from external evidence")
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ProofError("SQLite integrity_check failed")
        rows = connection.execute("SELECT id, nonce FROM proof").fetchall()
    except sqlite3.DatabaseError as error:
        raise ProofError("SQLite integrity/schema check failed") from error
    finally:
        if connection is not None:
            connection.close()
    if rows != [(1, nonce)]:
        raise ProofError("database nonce differs from external evidence")
    if file_hash(path) != sha256:
        raise ProofError("database hash changed during read-only recovery")
    return {"nonce": nonce, "sha256": sha256}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("initial", "recover"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--expected-nonce")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "initial":
            if args.expected_nonce or args.expected_sha256:
                raise ProofError("initial mode must not receive expected data")
            evidence = initial(args.database)
        else:
            evidence = recover(args.database, args.expected_nonce, args.expected_sha256)
        print(json.dumps(evidence, sort_keys=True), flush=True)
        while args.hold:
            signal.pause()
    except (OSError, ProofError, sqlite3.DatabaseError):
        print("SQLite proof failed; existing data was not replaced", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
