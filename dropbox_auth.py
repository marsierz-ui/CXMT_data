#!/usr/bin/env python3
"""
One-time Dropbox OAuth: turn an app key and secret into a refresh token.

Why this exists: Dropbox access tokens expire after 4 hours, so an unattended
job cannot hold one. A refresh token does not expire, and `dropbox_sync.py`
trades it for a fresh access token on every run. Getting that refresh token
needs a browser once - this script does the two steps around the browser.

Run it once, on this machine. Nothing here is needed again afterwards.

    python dropbox_auth.py                  # print the URL, take the code
    python dropbox_auth.py --gh-secrets     # ...and push the three GitHub secrets
    python dropbox_auth.py --check          # verify an existing refresh token

PREREQUISITE
    An app at https://www.dropbox.com/developers/apps
      - "Scoped access", access type "App folder": sandboxed under
        /Apps/<app name>/, unable to see the rest of the Dropbox, and writable
        on a team account where the team root is not. The alternative, Full
        Dropbox, needs --dest pointed at a member folder by hand.
      - Permissions tab: files.content.write, files.content.read,
        account_info.read. Submit the change before authorizing, or the token
        comes back without the scopes.

The app key and secret are read from the environment or .env, and prompted for
if absent. Neither the secret nor the refresh token is echoed to the terminal
unless you ask for it with --show.
"""

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropbox.com/oauth2/token"
ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"

ENV_FILE = Path(".env")
SECRET_NAMES = ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]


def load_dotenv(path=ENV_FILE):
    """Minimal KEY=VALUE reader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def post_form(url, fields, basic=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if basic:
        headers["Authorization"] = f"Basic {base64.b64encode(basic.encode()).decode()}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"[dropbox] {e.code}: {e.read().decode(errors='replace')}")


def app_credentials():
    key = os.environ.get("DROPBOX_APP_KEY") or input("App key: ").strip()
    secret = (os.environ.get("DROPBOX_APP_SECRET")
              or getpass.getpass("App secret (not echoed): ").strip())
    if not (key and secret):
        sys.exit("[auth] both an app key and an app secret are needed")
    return key, secret


def authorize(key, secret, open_browser):
    """The browser half: send the user to Dropbox, take back the code.

    No redirect_uri is registered, so Dropbox shows the authorization code on
    its own page for pasting back here. That keeps the whole flow to one
    script and one paste, with no local web server.
    """
    url = f"{AUTHORIZE}?" + urllib.parse.urlencode({
        "client_id": key,
        "response_type": "code",
        "token_access_type": "offline",   # this is what yields a refresh token
    })
    print("\n1. Open this URL, sign in, and click Allow:\n")
    print(f"   {url}\n")
    print("2. Dropbox shows an access code. Paste it below.\n")
    if open_browser:
        webbrowser.open(url)
    code = input("Code: ").strip()
    if not code:
        sys.exit("[auth] no code pasted")
    tok = post_form(TOKEN_URL, {"grant_type": "authorization_code", "code": code,
                                "client_id": key, "client_secret": secret})
    if "refresh_token" not in tok:
        sys.exit("[auth] Dropbox returned no refresh_token. The authorize URL "
                 "must carry token_access_type=offline; re-run this script.")
    print(f"\n[auth] ok - scopes: {tok.get('scope', '(none reported)')}")
    return tok["refresh_token"]


def refresh(key, secret, token):
    tok = post_form(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": token},
                    basic=f"{key}:{secret}")
    return tok["access_token"]


def whoami(access):
    req = urllib.request.Request(ACCOUNT_URL, data=b"null", method="POST",
                                 headers={"Authorization": f"Bearer {access}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"[dropbox] {e.code}: {e.read().decode(errors='replace')}")


def report(acct):
    name = acct.get("name", {}).get("display_name", "?")
    print(f"[check] account   : {name} <{acct.get('email')}>")
    root = acct.get("root_info", {})
    print(f"[check] root type : {root.get('.tag')} "
          f"(root={root.get('root_namespace_id')}, "
          f"home={root.get('home_namespace_id')})")
    if root.get(".tag") == "team":
        print("[check] a team account. An App-folder app writes to its own "
              "sandbox, so the default destination is fine. A Full Dropbox app "
              "needs DROPBOX_DEST set to a member folder, because the team "
              "root rejects writes.")


def write_env(key, secret, token):
    """Keep the three values in .env, which .gitignore already excludes."""
    keep = []
    if ENV_FILE.exists():
        keep = [ln for ln in ENV_FILE.read_text(encoding="utf-8").splitlines()
                if ln.split("=", 1)[0].strip() not in SECRET_NAMES]
    body = keep + [f"DROPBOX_APP_KEY={key}",
                   f"DROPBOX_APP_SECRET={secret}",
                   f"DROPBOX_REFRESH_TOKEN={token}"]
    ENV_FILE.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"[env] wrote {ENV_FILE.resolve()} (gitignored)")


def set_gh_secrets(values, repos):
    """Store the three secrets. Values go in on stdin, never argv.

    With no --repos this targets whatever repository the working directory
    is in, which is the normal case. Naming repositories fans the same
    token out to the sibling archives, which share one Dropbox app.
    """
    for repo in repos or [None]:
        where = ["--repo", repo] if repo else []
        for name in SECRET_NAMES:
            r = subprocess.run(["gh", "secret", "set", name, *where],
                               input=values[name], capture_output=True,
                               text=True)
            if r.returncode != 0:
                sys.exit(f"[gh] setting {name} on {repo or 'this repo'} "
                         f"failed:\n{r.stderr.strip()}")
            print(f"[gh] secret {name} set on {repo or 'this repo'}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="skip authorization; test the refresh token already set")
    ap.add_argument("--gh-secrets", action="store_true",
                    help="also store the three values as GitHub Actions secrets")
    ap.add_argument("--repos", nargs="*", default=[], metavar="OWNER/NAME",
                    help="with --gh-secrets, set them on these repositories "
                         "instead of the current one")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the URL without opening a browser")
    ap.add_argument("--show", action="store_true",
                    help="print the refresh token (otherwise it is only stored)")
    args = ap.parse_args()

    load_dotenv()
    key, secret = app_credentials()

    if args.check:
        token = os.environ.get("DROPBOX_REFRESH_TOKEN")
        if not token:
            sys.exit("[check] no DROPBOX_REFRESH_TOKEN in the environment or .env")
    else:
        token = authorize(key, secret, not args.no_browser)

    report(whoami(refresh(key, secret, token)))

    if args.check:
        print("[check] the refresh token works")
        if args.gh_secrets:
            set_gh_secrets({"DROPBOX_APP_KEY": key,
                            "DROPBOX_APP_SECRET": secret,
                            "DROPBOX_REFRESH_TOKEN": token}, args.repos)
        return

    write_env(key, secret, token)
    if args.show:
        print(f"[auth] refresh token: {token}")
    if args.gh_secrets:
        set_gh_secrets({"DROPBOX_APP_KEY": key, "DROPBOX_APP_SECRET": secret,
                        "DROPBOX_REFRESH_TOKEN": token},
                       args.repos)
        print("\nThe Actions mirror step turns itself on once these "
              "exist; an App-folder app needs no destination set, and "
              "uploads land in /Apps/<app name>/ANT_data. Watch the "
              "'Mirror to Dropbox' step of the next run to confirm.")
    else:
        print("\nTo let GitHub Actions upload unattended, re-run with "
              "--gh-secrets, or set the three secrets by hand under "
              "Settings -> Secrets and variables -> Actions.")


if __name__ == "__main__":
    main()
