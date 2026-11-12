# Core dependency audits

`../requirements.in` lists the direct dependencies. The legacy HTTP/RabbitMQ
adapters and the test runner remain supported in this update.

`../requirements.txt` is the Python 3.12 **Linux** deployment lock.
`../requirements-windows-py312.txt` is the Python 3.12 **Windows** lock for local
development and CLI use. Both locks pin the complete dependency tree and include
the exact selected wheel hashes for x86-64. Install the lock for the target platform:

```text
python -m pip install --require-hashes --only-binary=:all: -r imager/requirements.txt
python -m pip check
```

Use `requirements-windows-py312.txt` in the first command on Windows. Regenerate
each lock on its own target platform using Python 3.12 in a separate tooling
environment. Keep the reports and all pip caches outside the source checkout:

```text
python -m pip install --dry-run --ignore-installed --only-binary=:all: --report=PLAN.json -r imager/requirements.in
python imager/security/lock_from_report.py --full-plan --target=linux-x86_64 PLAN.json imager/requirements.txt
```

On Windows use `--target=windows-amd64` and the Windows lock filename. The
stdlib-only converter validates the report's platform, every distribution
name/version, matching binary wheel and SHA256. It emits no artifact URLs or
local paths. Pip's report does not record invocation flags: `--full-plan` is an
explicit assertion that the input was generated with `--ignore-installed`.
A fresh `--require-hashes` installation and `pip check` establish completeness;
missing transitive requirements must not be filled from an existing environment.
The generated requirements file is an installable lock; the JSON report is not.

The explicit `pip` requirement fixes the vulnerable installer inherited from the
base image. `urllib3` is declared directly because the MinIO adapter imports it.
The Starlette lower bound prevents re-resolution to the vulnerable versions
previously required by FastAPI 0.111.0.

Audits use pip-audit 2.10.1 in a separate tooling environment. Baseline data comes
from `pip freeze --all` in the stage-one Linux worker image at commit `262bafb`.
The sanitized baseline report records all distributions, advisory identifiers,
aliases, fixed versions and public advisory links. Duplicate advisory records
returned by the feed are merged by identifier; the original record count is
retained for comparison with pip-audit's console summary.

The baseline contained **20 unique CVEs across five packages** (35 feed records
including duplicates). The rebuilt Linux image has 49 installed distributions;
the fresh Windows environment has 51, including Windows-only dependencies.
Both installed-environment audits found **zero known advisories**, without
ignores or skipped packages. Exact inventories and the audited Linux image ID
are in `linux-installed.json` and `windows-installed.json`; `remediation.json`
maps every original CVE to the updated package version.

| Package | Before | Updated | Unique CVEs |
| --- | --- | --- | --- |
| pip | 25.0.1 | 26.3 | 6 |
| pymongo | 4.8.0 | 4.19.0 | 5 |
| pytest | 8.2.2 | 9.2.0 | 1 |
| python-dotenv | 1.0.1 | 1.2.4 | 1 |
| starlette | 0.37.2 | 1.10.0 | 7 |

Both target installations passed `pip check` and the complete offline core
suite: **233 tests and 199 subtests** on Linux and Windows. Linux ran without
network access or fallback packages. The legacy HTTP adapter also passed a no-network ASGI check for metrics,
schema rejection and routing valid JSON. Updated FastAPI requires a JSON
`Content-Type` header by default: legacy callers must send `application/json`.
The secure default is preserved.

The Linux report identifies the image used to audit the dependency installation
layer. A later source/documentation-only image refresh changes its Docker image
ID without changing that layer. Compare the recorded lock SHA256 and exact
installed inventory when checking an equivalent refresh.

Audit a complete installed-image inventory without resolving it on another OS:

```text
python -m pip_audit --disable-pip --no-deps -r linux-installed.txt --format=json --output=image-audit.json
```

Do not use `--no-deps` on `requirements.in`: it contains direct requirements only.
An installation report from `pip install --report` is evidence, not a lockfile.
Python-package auditing does not cover the base OS or native shared libraries.

Official references: [pip-audit](https://github.com/pypa/pip-audit),
[pip-tools](https://pip-tools.pypa.io/en/stable/),
[pip hash checking](https://pip.pypa.io/en/stable/topics/secure-installs/).
