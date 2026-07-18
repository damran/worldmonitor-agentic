"""Generate a filled `.env` from `.env.example` with strong random secrets — one command.

The first-boot pain this kills: the template ships ~12 `change-me` placeholders (a
non-development boot refuses every one of them, ADR 0061), each with its own constraint —
the Zitadel masterkey must be exactly 32 chars, the config-encryption key must be a valid
Fernet key, MinIO's app credentials must equal its root credentials, the Postgres/Redis DSNs
embed their passwords, and Zitadel's admin password must satisfy the default complexity
policy. Hand-editing those on a fresh host is exactly how first boots fail.

Usage (from the repo root; stdlib-only, so bare `python3` works too):

    uv run python scripts/dev/gen_env.py            # writes .env (refuses if it exists)
    uv run python scripts/dev/gen_env.py --force    # overwrite an existing .env

What it does NOT do: `ZITADEL_DOMAIN` / `ZITADEL_CLIENT_ID` still come from
`scripts/dev/zitadel_provision.sh` after first compose-up (they are minted by Zitadel, not
generatable here), and the opt-in blocks (PROJECTION_DIFF_*, WM_MCP_TOKEN, ...) stay as the
template documents them. Secret VALUES are never printed — only the key names.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER = "change-me"


def _password(n_bytes: int = 24) -> str:
    """A URL/DSN/compose-safe random secret (token_urlsafe alphabet: [A-Za-z0-9_-])."""
    return secrets.token_urlsafe(n_bytes)


def _fernet_key() -> str:
    """A valid Fernet key: urlsafe-base64 of 32 random bytes (no cryptography import needed)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _zitadel_admin_password() -> str:
    """Zitadel's default policy wants upper + lower + digit + symbol; guarantee all four."""
    return "Wm1!" + _password(12)


def generate(template: str) -> tuple[str, list[str]]:
    """Return (filled .env content, sorted key names that received generated values).

    Comments and every non-placeholder line pass through byte-identical, so the template's
    operator documentation survives into the generated file.
    """
    pg_password = _password()
    redis_password = _password()
    minio_password = _password()

    # Key-specific values, applied to `KEY=<anything containing change-me>` lines only —
    # the prose comments mentioning "change-me" are left untouched (they document the rule).
    specials: dict[str, str] = {
        "SESSION_SECRET_KEY": _password(),
        "CONFIG_ENCRYPTION_KEY": _fernet_key(),
        "POSTGRES_PASSWORD": pg_password,
        "POSTGRES_DSN": f"postgresql://worldmonitor:{pg_password}@localhost:5432/worldmonitor",
        "REDIS_PASSWORD": redis_password,
        "REDIS_URL": f"redis://:{redis_password}@localhost:6379/0",
        "NEO4J_PASSWORD": _password(),
        # MinIO: the app's S3 credentials MUST equal the root credentials (template contract),
        # and MINIO_ACCESS_KEY is already `worldmonitor` in the template — keep the user aligned.
        "MINIO_ROOT_USER": "worldmonitor",
        "MINIO_ROOT_PASSWORD": minio_password,
        "MINIO_SECRET_KEY": minio_password,
        # Exactly 32 characters (the template's own `openssl rand -hex 16` guidance).
        "ZITADEL_MASTERKEY": secrets.token_hex(16),
        "ZITADEL_ADMIN_PASSWORD": _zitadel_admin_password(),
    }

    generated: set[str] = set()
    out_lines: list[str] = []
    for line in template.splitlines(keepends=True):
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
        if match and _PLACEHOLDER in match.group(2):
            key = match.group(1)
            # Catch-all default: a NEW placeholder key added to the template later gets a
            # strong generic secret instead of surviving as change-me.
            value = specials[key] if key in specials else _password()
            out_lines.append(f"{key}={value}\n")
            generated.add(key)
        else:
            out_lines.append(line)
    return "".join(out_lines), sorted(generated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing .env (default: refuse)"
    )
    args = parser.parse_args(argv)

    example = _REPO_ROOT / ".env.example"
    target = _REPO_ROOT / ".env"
    if not example.is_file():
        print(f"REFUSED: {example} not found (run from a repo checkout)", file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(
            f"REFUSED: {target} already exists — it may hold live secrets/operator edits. "
            "Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return 2

    content, generated = generate(example.read_text(encoding="utf-8"))
    target.write_text(content, encoding="utf-8")
    os.chmod(target, 0o600)

    print(f"wrote {target} (mode 600) with generated secrets for {len(generated)} keys:")
    for key in generated:
        print(f"  {key}")
    print(
        "\nnext: docker compose -f deploy/compose.yaml --env-file .env up -d\n"
        "then: ./scripts/dev/zitadel_provision.sh  "
        "(paste the printed ZITADEL_DOMAIN / ZITADEL_CLIENT_ID into .env)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
