"""Settings ⊆ .env.example drift guard (rereview 2026-07-11 finding #5).

Every field on :class:`worldmonitor.settings.Settings` must appear in ``.env.example`` as an
(optionally commented-out) ``KEY=`` line, so a new setting cannot land without operator
documentation. The finding's failure mode: two boot-halting secrets (``SESSION_SECRET_KEY``,
``ZITADEL_CLIENT_SECRET``) were absent from the template, turning the first non-development
boot into an undocumented fail-closed halt.
"""

import re
from pathlib import Path

from worldmonitor.settings import Settings

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
# An uppercase KEY= at line start, optionally commented out ("# KEY="). Prose comment lines
# never match: after "#" + whitespace the key name must run uninterrupted into "=".
_KEY_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def test_every_settings_field_documented_in_env_example() -> None:
    documented = set(_KEY_RE.findall(_ENV_EXAMPLE.read_text(encoding="utf-8")))
    fields = {name.upper() for name in Settings.model_fields}
    missing = sorted(fields - documented)
    assert not missing, f".env.example is missing Settings fields: {missing}"


def test_boot_halting_secrets_present_uncommented() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("SESSION_SECRET_KEY", "ZITADEL_CLIENT_SECRET"):
        assert re.search(rf"^{key}=", text, re.MULTILINE), (
            f"{key} must be an uncommented line in .env.example "
            "(non-development boots halt on it, ADR 0061/0068)"
        )
