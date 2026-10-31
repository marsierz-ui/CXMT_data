#!/usr/bin/env python3
"""
Mirror collected data files to Dropbox.

Why: GitHub measures a repository by its whole history, and a master CSV that
is rewritten on every poll stores a fresh blob each time. Text CSVs delta well
so the growth is slow, but 1m series grow without bound and gzipped tick files
cannot be delta-compressed at all -
[SPCX_data](https://github.com/marsierz-ui/SPCX_data) is at ~3.5 GB against
GitHub's 5 GB hard limit for that reason. Dropbox has no history-amplification
problem: overwriting a file costs its size, not its size times the number of
revisions. So the finest granularity can live there in full while git keeps
only what is comfortable.

The same file is used by ANT_data, SPCX_data, CBRS_data and CXMT_data; what
differs is the --src/--glob/--dest its workflow passes.

AUTH (one-time setup, see README and dropbox_auth.py)
    Dropbox access tokens expire after 4 hours, so this uses a refresh token,
    which does not expire. Three values are needed:

        DROPBOX_APP_KEY
        DROPBOX_APP_SECRET
        DROPBOX_REFRESH_TOKEN

    Read from the environment, or from a local .env file (gitignored).
    In GitHub Actions, add them under
    Settings -> Secrets and variables -> Actions.

USAGE
    python dropbox_sync.py                            # out/*_master.csv -> /ANT_data
    python dropbox_sync.py --dest /archive/ANT        # somewhere else
    python dropbox_sync.py --glob "*_1m_master.csv"   # only the finest series
    python dropbox_sync.py --src . --glob "data/**/*" --dest /SPCX_data
    python dropbox_sync.py --src . --glob "out/*.csv" "data/**/*" --dest /CBRS_data
    python dropbox_sync.py --dry-run                  # show what would upload

Paths under --src are preserved below --dest, so `--src . --glob "data/**/*"`
reproduces the data/ tree in Dropbox rather than flattening it into one folder.

Unchanged files are skipped: Dropbox exposes its own content_hash, which this
recomputes locally and compares before uploading. Repeated runs therefore cost
one metadata call per file and no bandwidth - which is what makes mirroring a
few hundred megabytes of immutable tick archives on every poll practical.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://api.dropbox.com/oauth2/token"
META_URL = "https://api.dropboxapi.com/2/files/get_metadata"
UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
SESSION_START = "https://content.dropboxapi.com/2/files/upload_session/start"
SESSION_APPEND = "https://content.dropboxapi.com/2/files/upload_session/append_v2"
SESSION_FINISH = "https://content.dropboxapi.com/2/files/upload_session/finish"

BLOCK = 4 * 1024 * 1024        # Dropbox content_hash block size, also the chunk size
SIMPLE_UPLOAD_MAX = 150 * 1024 * 1024


def load_dotenv(path=Path(".env")):
    """Minimal KEY=VALUE reader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def content_hash(path):
    """Dropbox content_hash: sha256 over the concatenated sha256 of 4 MiB blocks."""
    digests = b""
    with open(path, "rb") as f:
        while chunk := f.read(BLOCK):
            digests += hashlib.sha256(chunk).digest()
    return hashlib.sha256(digests).hexdigest()


def request(url, data, headers, method="POST"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read()
    return json.loads(body) if body else {}


def access_token():
    key = os.environ.get("DROPBOX_APP_KEY")
    secret = os.environ.get("DROPBOX_APP_SECRET")
    refresh = os.environ.get("DROPBOX_REFRESH_TOKEN")
    if not (key and secret and refresh):
        sys.exit("[auth] set DROPBOX_APP_KEY, DROPBOX_APP_SECRET and "
                 "DROPBOX_REFRESH_TOKEN (environment or .env); see README")
    body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": refresh}).encode()
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    try:
        tok = request(TOKEN_URL, body,
                      {"Authorization": f"Basic {auth}",
                       "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        sys.exit(f"[auth] token refresh failed: {e.code} "
                 f"{e.read().decode(errors='replace')}")
    return tok["access_token"]


def remote_hash(token, dest):
    """content_hash of dest on Dropbox, or None if it is not there."""
    try:
        meta = request(META_URL, json.dumps({"path": dest}).encode(),
                       {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"})
        return meta.get("content_hash")
    except urllib.error.HTTPError as e:
        if e.code == 409:          # path/not_found
            return None
        raise


def explain(e, dest):
    body = e.read().decode(errors="replace")
    if "team_folder" in body or "not_writable" in body:
        return (f"{e.code}: cannot write {dest} - a Dropbox team account's root "
                f"is not writable. Register the app with 'App folder' access so "
                f"its paths are sandboxed, or point --dest at your own member "
                f"folder (e.g. '/Your Name/ANT_data'). ({body})")
    return f"{e.code} {e.reason}: {body}"


def upload_simple(token, path, dest):
    arg = json.dumps({"path": dest, "mode": "overwrite",
                      "autorename": False, "mute": True})
    request(UPLOAD_URL, path.read_bytes(),
            {"Authorization": f"Bearer {token}",
             "Dropbox-API-Arg": arg,
             "Content-Type": "application/octet-stream"})


def upload_session(token, path, dest):
    """Chunked upload for files over Dropbox's 150 MB single-request limit."""
    hdr = {"Authorization": f"Bearer {token}",
           "Content-Type": "application/octet-stream"}
    with open(path, "rb") as f:
        first = f.read(BLOCK)
        sess = request(SESSION_START, first,
                       {**hdr, "Dropbox-API-Arg": json.dumps({"close": False})})
        sid, offset = sess["session_id"], len(first)
        while chunk := f.read(BLOCK):
            arg = {"cursor": {"session_id": sid, "offset": offset}, "close": False}
            request(SESSION_APPEND, chunk,
                    {**hdr, "Dropbox-API-Arg": json.dumps(arg)})
            offset += len(chunk)
    arg = {"cursor": {"session_id": sid, "offset": offset},
           "commit": {"path": dest, "mode": "overwrite",
                      "autorename": False, "mute": True}}
    request(SESSION_FINISH, b"",
            {**hdr, "Dropbox-API-Arg": json.dumps(arg)})


def collect(src, patterns):
    """Files matching any pattern, deduped, with their paths relative to src.

    A ** pattern also matches directories, hence the is_file() filter; and one
    file can match two patterns, hence the set.
    """
    found = {p for pat in patterns for p in src.glob(pat) if p.is_file()}
    return sorted((p.relative_to(src).as_posix(), p) for p in found)


def sync(files, dest_root, dry_run):
    token = None if dry_run else access_token()
    root = dest_root.rstrip("/")
    uploaded = skipped = sent = 0
    for rel, path in files:
        dest = f"{root}/{rel}"
        size = path.stat().st_size
        if dry_run:
            print(f"[dry-run] would upload {rel} ({size / 1048576:.2f} MB) -> {dest}")
            uploaded += 1
            continue
        if remote_hash(token, dest) == content_hash(path):
            print(f"[skip] {rel} unchanged")
            skipped += 1
            continue
        try:
            if size > SIMPLE_UPLOAD_MAX:
                upload_session(token, path, dest)
            else:
                upload_simple(token, path, dest)
        except urllib.error.HTTPError as e:
            sys.exit(f"[up] {rel}: {explain(e, dest)}")
        print(f"[up] {rel} ({size / 1048576:.2f} MB) -> {dest}")
        uploaded += 1
        sent += size
    print(f"=== {uploaded} uploaded ({sent / 1048576:.1f} MB), "
          f"{skipped} unchanged ===")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="out", help="local directory to mirror")
    ap.add_argument("--glob", nargs="+", default=["*_master.csv"],
                    help="one or more patterns relative to --src; ** recurses")
    ap.add_argument("--dest", default="/ANT_data", help="Dropbox folder")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    src = Path(args.src)
    if not src.is_dir():
        sys.exit(f"[src] {src} is not a directory")
    files = collect(src, args.glob)
    if not files:
        sys.exit(f"[src] nothing matching {args.glob} in {src}/")
    total = sum(p.stat().st_size for _, p in files) / 1048576
    print(f"=== dropbox sync: {len(files)} files ({total:.1f} MB) "
          f"{src}/ -> {args.dest} ===")
    sync(files, args.dest, args.dry_run)


if __name__ == "__main__":
    main()
