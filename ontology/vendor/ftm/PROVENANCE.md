# FtM Schema Vendor Provenance

**Upstream package:** followthemoney (PyPI)
**Exact vendored version:** 4.9.2
**Retrieval date:** 2026-07-04

## Purpose

These 69 YAML files are vendored L2 schema data (not imported at runtime). They are the
authoritative snapshot of the FollowTheMoney ontology at version 4.9.2, guarded by the
`ftm-schema` CI gate (ADR 0098). Any divergence between the installed package schema and
these vendored files causes the gate to fail, making L2 contract drift visible at build time.

## Re-vendoring on upgrade

When upgrading followthemoney, run the following to re-vendor the schema:

```sh
cp $(uv run python -c "import followthemoney, os; print(os.path.join(os.path.dirname(followthemoney.__file__), 'schema'))")/*.yaml ontology/vendor/ftm/
```

Then update:
1. The `==` pin in `pyproject.toml` to the new version
2. The version reference in this `PROVENANCE.md` file
3. Run `uv lock` to update the lockfile
4. Run `uv run python scripts/check_ftm_schema.py` to confirm the gate is green
