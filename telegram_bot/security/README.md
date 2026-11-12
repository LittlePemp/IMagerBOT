# Bot dependency verification

The runtime locks target CPython 3.12 on Linux x86-64 (`../requirements.txt`)
and Windows AMD64 (`../requirements-windows-py312.txt`). They contain the full
native dependency tree and the exact selected wheel hashes. Install the lock
for the target platform with `--require-hashes --only-binary=:all:`.

Both clean native installations passed `pip check` and installed-inventory
pip-audit 2.10.1 checks: **33 Linux packages / 35 Windows packages, zero known
advisories, no skipped packages or ignores**. These are isolated runtime
environments; no new bot image, Compose stack or Telegram polling was started.

The old 30-entry requirements inventory produced **92 advisory records across
8 packages**. Duplicate feed records are retained in that count; they represent
54 distinct advisory identifiers and 53 distinct CVE identifiers, not 92 unique
CVEs. `baseline.json` contains sanitized identifiers and public advisory links.
`linux-installed.json` and `windows-installed.json` record the clean runtime
inventories and lock hashes. `summary.json` records the package actions, exact
test commands, import paths and verification limits.

Aiogram 3.31.0 requires Pydantic below 2.14, so the bot uses Pydantic 2.13.5.
Motor 3.7.1 temporarily preserves the existing database API with PyMongo 4.19.0.
Unused RabbitMQ and httpcore dependencies are absent from the new resolution.

All **15 existing tests passed on both platforms**, using the fresh packages
through `IMAGER_TEST_DEPS`; the runner disables legacy `.packages` fallback
when that variable is set. Pytest 9.2.0 was installed only for verification and
is excluded from the runtime locks. Linux tests ran without network access,
with `--capture=sys` to avoid temporary-file capture issues on Windows bind
mounts. All temporary files, caches and environments stayed on the E: runtime
directory; one-off containers were limited to 768m memory and one CPU.

The sole warning is the existing class-based Pydantic `Config` deprecation in
`ImageGroup`. No handler, architecture or compatibility-code changes were needed.
