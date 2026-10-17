# Vendored production-audit validator

Copied unchanged from mdn87/lugos at commit
`f85c3271caffa5f97e3958de65bb7f5d18c1b8fa`:

- `plugins/lugos-toolset/skills/production-audit/scripts/validate_report.py`
  as `scripts/validate_report.py`
- `plugins/lugos-toolset/skills/production-audit/references/concepts.md`
  as `references/concepts.md`

Identity: skill_version `1.1.0`, catalog_digest
`df08226bb8e2414719e14b1c1a21fc40b63587793c1c3499091b6073ad906dea`.

The JSON report format is the contract between the skill and Cirra, so Cirra
validates reports with its own copy rather than a lugos checkout. Updating this
copy is a deliberate Cirra commit: copy both files from one lugos commit, record
that commit and the new identity here, and keep the files byte-identical (they
are excluded from Ruff for that reason). `import-audit` refuses reports whose
catalog_digest differs from this copy's.
