# AGENTS.md

## Resolute migration project

This checkout's `resolute-dev` branch migrates the Noble baseline to Ubuntu
Resolute (26.04, Python 3.14, PostgreSQL 18). Apply this section alongside the
general Launchpad guidance below. The `noble-dev` branch remains the Noble
baseline.

### Environment

- Run builds, tests, package builds and Python tools inside the `lp-resolute`
  LXC container as `finnrg`, not root. Only the checkout and dependency
  directory are mounted at their host paths; the container has its own
  home directory. Host Python is not the target runtime.
- The checkout is `/home/finnrg/launchpad-noble/launchpad`. For example:

  ```sh
  lxc exec lp-resolute -- runuser -u finnrg -- sh -c \
    'cd /home/finnrg/launchpad-noble/launchpad && bin/test -m lp.services.webauthn -vv'
  ```

- Use the checkout's `env/` and `bin/`, not system Python packages. Launchpad
  startup hooks must initialize the environment, including
  `STORM_CEXTENSIONS=1`. If they are missing, repair the build/startup hooks;
  do not mask the problem by exporting that variable alone.
- The previous Noble environment and wheels are retained in `env-noble/`
  and `wheels-noble/`. Do not reuse their native extensions on Python 3.14
  or rebuild the shared checkout from `lp-noble` during Resolute work.
- The development container has accumulated build dependencies and fixture
  state. A successful local build does not prove a fresh CI setup works.
  Declare required system packages in `utilities/rocketfuel-setup`.
- The Launchpad PPA has no Resolute suite. The setup script uses native
  Resolute packages and PostgreSQL 18, allowing only `convoy-common`,
  `python3-convoy` and `geoip-data-city-lite` from the Noble PPA.
- Avoid broad cleanup in this shared environment. `make clean` removes
  shared `/var/tmp` fixture paths; use targeted cleanup and do not disrupt
  another test run or rebuild databases without checking first.

### Migration plan and compatibility

1. Keep a reproducible offline dependency install and clean Resolute build.
2. Resolve remaining migration failures in small, logical groups. Reproduce
   the original error, fix its shared cause where possible, and run all
   relevant focused tests. Do not weaken assertions or disable security
   checks just to obtain a pass.
3. Use full-suite CI to identify remaining failures after focused validation.
   Separate application defects, changed expectations, missing dependencies,
   fixture failures and tests that never ran.
4. Move toward current upstream Twisted without a permanent Launchpad fork.
   Preserve existing SSH authentication and key exchange throughout.

- Post-quantum `mlkem768x25519-sha256` support is mandatory, alongside
  Ed25519, RSA, ECDSA and OpenSSH security-key authentication.
- The inherited SSH stack uses Twisted `26.10.0+lp2` and lazr.sshserver
  `0.1.16+lp2`. Their source branches are `../twisted`
  (`lp-backport-26.10.0`) and `../lazr.sshserver`
  (`noble-twisted-26.10`).
- Noble's system OpenSSL 3.0 cannot provide the upstream cryptography ML-KEM
  backend. The temporary Twisted fork uses the separate `ml-kem-rs` package.
  Keep that delta small; do not remove it until the replacement backend is
  verified. See the [SSH compatibility report](NOBLE_SSH_COMPATIBILITY_REPORT.md).

### Dependencies, testing and evidence

- `download-cache` points to the separate Git repository at
  `../lp-sourcedeps/download-cache`. Its branch is also `resolute-dev`.
  Put archives in `download-cache/dist/`; prefer sdists over wheels unless
  source builds cause substantial difficulty. Keep installs offline.
- Update dependency pins in `requirements/launchpad.txt`; declare new direct
  requirements in `setup.cfg`. Do not hand-edit generated
  `requirements/combined.txt`.
- Python 3.14 dependency changes use environment markers to preserve older
  runtime pins. The cache contains source fixes for BSON packaging and
  fastbencode's native integer and recursion handling. Keep its native
  extension working: missing-extension warnings prevent daemon test setup.
- API documentation workers explicitly use `fork` to inherit Zope
  registrations. Check worker exit statuses and generated WADL, JSON and HTML
  for every active API version.
- PostgreSQL 18 rejects the historical `debversion >` negator. Schema setup
  corrects a generated copy in `build/schema/`; do not edit the production
  baseline or weaken restore error checking.
- When proving source builds work, bypass pip's wheel cache explicitly.
  `PIP_NO_CACHE_DIR=1` alone was overridden by the Makefile's `--cache-dir`.
- Keep Resolute results separate from the Noble
  [test checklist](NOBLE_TRANSITION_TEST_TODO.md). The
  [original test report](NOBLE_TRANSITION_TEST_REPORT.md) is historical
  evidence, not the current failure inventory.
- Check off only tests observed passing, retaining exact runtime IDs and
  scenario suffixes. Missing results, skipped tests and layer-setup failures
  are not passes. Record the tested commit, command/scope and evidence.
- The original audit included all test levels; CI's `bin/test -vvc` uses
  level 1. Do not compare their totals as if the selected scope were equal.
- A newer dependency can fix many downstream tests at once. Distinguish an
  observed pass from a proven explanation for why it now passes.

### Commits and remote changes

- Preserve unrelated local changes, including edits in files you touch.
  When asked to commit, separate logical changes and stage only those changes. Always explain the "Why?" in the commit message.
  Do not add AI co-author trailers.
- CI is in `FinnRG/launchpad-private`; its push target is
  `gh-private/resolute-dev`, not the branch's older tracking remote.
  CI defaults to an Ubuntu 26.04 container and the `resolute-dev` dependency
  branch; the GitHub host runner remains Ubuntu 24.04.
  The dependency cache is published separately to its `finnrg` remote.
- Do not authenticate to Launchpad, push, force-push, publish packages or
  change remote state without explicit approval for that action. Preparing
  and testing a change does not authorize publishing it.

Guidance for AI agents working in the Launchpad codebase.

## Project

Launchpad is Canonical's collaboration and code-hosting platform (Python 3,
Zope 3 / lazr / Storm ORM, PostgreSQL). It is a large monolith served via
WSGI/gunicorn with Twisted-based daemons and cron scripts.

Full docs: https://documentation.ubuntu.com/launchpad/en/latest/

## Related repositories (full stack)

This repo is the main Launchpad web application; the full service is split
across several separate git repos, all part of the
[Launchpad Suite](https://launchpad.net/launchpad-project) (AGPLv3) and
browsable under `git.launchpad.net`:

- `launchpad-buildd` — builder service that runs on the Launchpad build-farm
  machines. https://git.launchpad.net/launchpad-buildd
- `turnip` — flexible, scalable Git server suite (Python/Twisted) that backs
  Git code hosting. https://git.launchpad.net/turnip
- `txpkgupload` — Twisted package-upload server (formerly "poppy") handling
  FTP/SFTP uploads. https://git.launchpad.net/txpkgupload
- `lp-codeimport` — the code-import worker that mirrors foreign VCS branches.
  https://git.launchpad.net/lp-codeimport
- `launchpad-mojo-specs` — Mojo deployment specifications used to deploy the
  Launchpad services. 
  https://git.launchpad.net/launchpad-mojo-specs

Shared libraries and tools live in their own repos and are consumed via
`download-cache` — e.g. `storm` (ORM), `launchpadlib` / `lazr.restfulclient`
(web-API client) and the `lazr.*` family. 

## Environment

- Development runs inside the supported LXC container; most `make`/`bin/*`
  targets assume that setup plus a local PostgreSQL.
- Dependencies come from `download-cache` (pip runs with `PIP_NO_INDEX=1`); do
  not add packages by pulling from PyPI. Declare direct deps in `setup.cfg`.
- The virtualenv lives in `env/` and `bin/` is symlinked from it after a build.

## Common commands

```sh
make                 # default: build in place (compile + apidoc + jsbuild + css)
make schema          # (re)create the launchpad_dev database and test template
make run             # run the app locally (LPCONFIG=development, 127.0.0.88)
make clean           # remove build artifacts
```

Run the app under `LPCONFIG` (default `development`); config instances live in
`configs/<instance>/launchpad-lazr.conf`.

## Testing

Tests use `zope.testrunner` via `bin/test`. Most tests need the test database
(`make schema` builds `launchpad_ftest_template`).

The suite is huge — don't run it all. Scope each run to the relevant
module(s)/test(s) with `-m`/`-t`, but run every test relevant to your change,
not just one.

```sh
bin/test -m lp.registry -t TestPerson   # narrow by module and/or test name
bin/test -vvt test_config -m lp.services.config.tests  # verbose run of one module
```

- `-m <dotted.package>` limits scope; `-t <name>` matches test class/method
  (regex). `-p` shows progress, `-vv` is verbose.
- Test layers are defined in `lib/lp/testing/layers.py`. Pick the lightest one
  that works: `BaseLayer` (no resources) → `DatabaseFunctionalLayer` →
  `LaunchpadFunctionalLayer`, etc. Set `layer = ...` on the `TestCase`.
- Base classes: `lp.testing.TestCase` / `TestCaseWithFactory` (use
  `self.factory` to build objects). See `standard_test_template.py`.
- Assertions take expected value first: `self.assertEqual(expected, actual)`.
- Unit/functional tests live in each package's `tests/`; browser "page tests"
  in `stories/`; doctests in `doc/*.rst`.

## Code layout

- `lib/lp/<app>/` — application domains (`registry`, `bugs`, `code`, `soyuz`,
  `blueprints`, `answers`, `translations`, `snappy`, `oci`, `charms`, ...).
- `lib/lp/services/` — cross-cutting infrastructure (database, webapp, config,
  librarian, job, mail, features, gpg, crypto, ...).
- `lib/lp/app/` — shared web-app framework code; `lib/canonical/` — legacy base.
- `database/schema/` — SQL baseline and patches; `database/sampledata/`.
- `cronscripts/`, `scripts/`, `daemons/` — batch jobs and long-running services.
- `configs/` — per-instance runtime configuration selected by `LPCONFIG`.
- `charm/`, `utilities/`, `brzplugins/` — deployment, dev tooling, Bazaar plugins.

Typical `lib/lp/<app>/` package: `interfaces/` (Zope interfaces),
`model/` (Storm implementations), `browser/` + `templates/` (views/pages),
`tests/`, `stories/`, `doc/`, `configure.zcml`, `security.py`, `enums.py`.

## Database patches

Schema changes ship as numbered SQL files in `database/schema/`, applied in
filename order on top of the `launchpad-2211-00-0.sql` baseline.

- **Name** the file `patch-2211-NN-P.sql`:
  - `2211` is the current baseline major — do not change it.
  - `NN` is a two-digit minor; for a new branch pick the next unused minor
    (one higher than the highest existing `patch-2211-*`).
  - `P` is a single-digit patch within that minor; start at `0` and use
    `1`, `2`, … for follow-ups on the same branch. `upgrade.py` globs
    `patch-????-??-?.sql`, so the minor is exactly two digits and the patch a
    single digit (0–9).
- **Template** (note the SQL `--` header, not `#`):

  ```sql
  -- Copyright <YEAR> Canonical Ltd.  This software is licensed under the
  -- GNU Affero General Public License version 3 (see the file LICENSE).

  SET client_min_messages=ERROR;

  -- DDL: CREATE TABLE / ALTER TABLE / CREATE INDEX ...

  INSERT INTO LaunchpadDatabaseRevision VALUES (2211, NN, P);
  ```

  The closing `INSERT INTO LaunchpadDatabaseRevision VALUES (major, minor,
  patch);` is mandatory and must match the numbers in the filename.
- **New tables/columns** need matching changes elsewhere:
  - `database/schema/security.cfg` — add `public.<table> = SELECT, INSERT,
    UPDATE, DELETE` (lowercased) in the default section, plus entries under any
    per-service `[section]` that reads or writes it; `security.py` applies
    these grants.
  - Add `COMMENT ON TABLE`/`COLUMN` statements directly inside the SQL patch
    file itself (do not append them to `database/schema/comments.sql`).
  - A Storm model under `lib/lp/<app>/model/` inheriting `StormBase`.
- **Large/production tables**: add CHECK and FK constraints as `NOT VALID` to
  avoid long locks, and put anything that must run `CONCURRENTLY` in production
  in a separate follow-up "hot patch" (`-1`, `-2`, …) marked with a
  `-- To be done CONCURRENTLY` comment.
- **Apply and test**: `make schema` rebuilds `launchpad_dev` and
  `launchpad_ftest_template`, running `upgrade.py` (patches) then `security.py`
  (grants); `database/schema/upgrade.py -d <db>` patches an existing database.

## Conventions

- Every source file starts with the license header:

  ```python
  # Copyright <YEAR> Canonical Ltd.  This software is licensed under the
  # GNU Affero General Public License version 3 (see the file LICENSE).
  ```

- Modules declare a docstring then `__all__`. See `standard_template.py`.
- Write comments and docstrings in plain, direct English. Prefer simple,
  widely understood wording over jargon or nouns pressed into use as verbs.
  A comment should be clear on the first read.
- Keep content clean and low-noise. The comments, docstrings, and commit
  messages produced must read as if a careful human wrote them. The few minutes
  the author spends cleaning this up saves far more time for every reviewer and
  everyone who reads the code later. Keep it concise.
  - Do not add the thinking process, narration, or step-by-step reasoning as
    comments or commit messages. Explain what the code does and why, not how
    you arrived at it.
  - Cut over-explanation. Comment only what is non-obvious; delete restatements
    of the code, comments that teach general concepts, and defensive caveats no
    reader needs.
  - Do not leak the prompting context. Reference files, examples, or tickets to
    guide an implementation do not belong in the comment or commit message.
    Describe the change on its own terms.
  - Avoid vague, hard-to-parse phrasing. If a comment would make
    the next human stop and decode it, rewrite it in direct language.
  - Keep commit messages tight: a clear summary line and only the context a
    reviewer actually needs. Reviewers should not have to wade through walls of
    generated prose to find the point.
  - Use single backticks in Python code comments.
- Formatting/linting is enforced by pre-commit; run before committing:

  ```sh
  tox -e lint     # black, isort (profile=black), flake8, pyupgrade,
                  # eslint, shellcheck, codespell, bandit, doctest lint
  tox -e mypy     # type-check the subset listed in tox.ini
  ```

  Line length is **79**. Target Python 3.8+ syntax (pyupgrade `--py38-plus`).
- Imports must be absolute (`flake8-absolute-import`).
- Storm models inherit from `lp.services.database.stormbase.StormBase`, **not**
  `storm.base.Storm` / `storm.locals.Storm` (flake8 rejects the latter).
- For YUI JavaScript modules, follow
  `lib/lp/code/javascript/branchmergeproposal.requestmerge.js` (and its
  tests in `lib/lp/code/javascript/tests/`) as the reference example of
  code style — arrow functions, `const`/`let`, JSDoc typedefs with
  references to the Python source of the payloads, and a header comment
  plus section comments describing the module's states — rather than the
  older modules in the tree.
  - Shared editor types for YUI and Launchpad globals live in
    `lib/lp/app/javascript/yui.d.ts`. Import them at the top of JavaScript
    modules with JSDoc `@import`, adjusting the relative path, for example:

    ```javascript
    /** @import {LaunchpadClient, YuiNode} from "../../app/javascript/yui" */
    ```

    Extend the shared declaration when another module needs a common YUI API;
    keep payload types that are specific to one module beside that module.
  - Avoid ES6 template literals (backtick strings, including `${...}`
    interpolation) in these modules for now; a bug in the `jsbuild`
    pipeline mangles them. Use single-quoted strings with `+`
    concatenation until that is fixed.
  - When you touch an older JavaScript module for another change,
    opportunistically modernize the parts you edit toward this style
    (arrow functions, `const`/`let`, JSDoc typedefs, header/section
    comments) so the tree improves gradually. Keep the refactor scoped
    to what you are already changing — do not rewrite whole unrelated
    files in one pass.
- Web-facing changes usually require matching `.zcml` registration and a
  `security.py` entry.
- `configs/+*` directories are throwaway local configs and are ignored; do not
  commit config containing credentials or host-specific data.
