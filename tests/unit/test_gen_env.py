"""`scripts/dev/gen_env.py` — the generated .env satisfies every template constraint.

Runs the pure `generate()` over the REAL `.env.example`, so template drift (a new
`change-me` key, a renamed field) is caught here: nothing placeholder may survive, and each
special-cased constraint (32-char masterkey, valid Fernet key, MinIO root==app credentials,
passwords embedded in the DSNs, Zitadel complexity) must hold on the actual template.
"""

from __future__ import annotations

import base64
import importlib.util
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("gen_env", _REPO / "scripts" / "dev" / "gen_env.py")
assert _spec is not None and _spec.loader is not None
gen_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_env)

_TEMPLATE = (_REPO / ".env.example").read_text(encoding="utf-8")


def _values(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_no_placeholder_survives_and_constraints_hold() -> None:
    content, generated = gen_env.generate(_TEMPLATE)
    values = _values(content)

    # No assignment line still carries the placeholder (comment prose may mention it).
    for key, value in values.items():
        assert "change-me" not in value, f"{key} still carries the placeholder"
    assert len(generated) >= 12, f"expected >=12 generated keys, got {generated}"

    assert len(values["ZITADEL_MASTERKEY"]) == 32
    # A valid Fernet key: urlsafe-b64 decoding to exactly 32 bytes.
    assert len(base64.urlsafe_b64decode(values["CONFIG_ENCRYPTION_KEY"])) == 32
    assert values["MINIO_SECRET_KEY"] == values["MINIO_ROOT_PASSWORD"]
    assert values["MINIO_ACCESS_KEY"] == values["MINIO_ROOT_USER"] == "worldmonitor"
    assert values["POSTGRES_PASSWORD"] in values["POSTGRES_DSN"]
    assert values["REDIS_PASSWORD"] in values["REDIS_URL"]
    admin = values["ZITADEL_ADMIN_PASSWORD"]
    assert (
        any(c.isupper() for c in admin)
        and any(c.islower() for c in admin)
        and any(c.isdigit() for c in admin)
        and any(not c.isalnum() for c in admin)
    ), "Zitadel default policy needs all four character classes"


def test_non_placeholder_lines_pass_through_byte_identical() -> None:
    content, _ = gen_env.generate(_TEMPLATE)
    original = _TEMPLATE.splitlines()
    result = content.splitlines()
    assert len(original) == len(result)
    for before, after in zip(original, result, strict=True):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", before)
        if not (m and "change-me" in m.group(2)):
            assert before == after, f"non-placeholder line altered: {before!r}"


def test_two_runs_generate_different_secrets() -> None:
    a, _ = gen_env.generate(_TEMPLATE)
    b, _ = gen_env.generate(_TEMPLATE)
    assert _values(a)["NEO4J_PASSWORD"] != _values(b)["NEO4J_PASSWORD"]
