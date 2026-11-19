# Handover: aviso-core, branch `migrate-endpoints`

Written 2026-11-16. Covers everything on this branch that is not on `master`:
committed, uncommitted and untracked.

The companion document is `docs/handover.md` in the `aviso-infrastructure`
repository, which provides the `aviso` library this service installs as a
wheel. Read both: several changes only work when the two ship together.

For setting up a machine to run the service, see `README.md` in this
repository. This document does not repeat it.

## 1. Status at a glance

| | |
| --- | --- |
| Branch | `migrate-endpoints` at `4ad0e13` (tracks `usercopy/migrate-endpoints`) |
| Base | `origin/master` at `aedd48b`; merge-base `3f7962a` |
| Ahead / behind master | 21 commits ahead, 5 behind |
| Uncommitted | Edits to 18 tracked files, about 380 added lines |
| Untracked | 6 new files, about 760 lines (listed in section 3) |
| `aviso` wheel | `wheels/aviso-0.1.13-py3-none-any.whl`, staged but not committed; the last commit still has `aviso-0.1.15` |

Three things to know before anything else:

- **A large part of the work is not committed.** Four of the migrated endpoint
  modules, the sandbox shell verbs and one test file exist only as untracked
  files, and `aviso_core/urls.py` and `gbm_apis/urls.py` already import them.
  A checkout of the last commit does not contain them.
- **Master has moved.** The 5 commits on master that this branch lacks are the
  renewal-deals deletion handling (CS-44196) and the streaming change that
  reduces memory use in `deals_results`. They touch
  `gbm_apis/api/data_load.py` and `gbm_apis/api/deals_results.py`, which this
  branch did not change. Merge master before review. Diff with three dots so
  those commits are not misread as removals:

  ```
  git diff origin/master...HEAD    # what the branch committed
  git diff HEAD                    # uncommitted edits
  git status --short               # untracked files
  ```
- **The wheel includes uncommitted aviso-infrastructure work.** See that
  repository's handover, section 1.

## 2. Background: what this branch is for

Until now the only way to call aviso-core was with a shared secret in an
`Internal-Api-Key` header. The legacy GBM service is reached differently:
people and services log in through the Aviso SDK (`avisosdk`), get a shell
object, and call named shell commands or `shell.api(...)`.

This branch makes aviso-core reachable the same way, so that it can stand in
for the legacy GBM service:

1. Login, session and tenant-switch endpoints the SDK expects.
2. The SDK package the client downloads at login, built and served by this
   service.
3. The shell commands and the endpoints behind them that the legacy GBM shell
   offered and that callers still use: dataset, sandbox, tenant config, period
   boundaries, authorization tokens.
4. The image and startup changes those need.

Terms: a **stack** is one deployment of the GBM microservice with its own
Postgres database (`gbm1-ms`, `gbm2-ms`). **FM** is the main application
database. aviso-core serves tenants on every stack from one deployment.

## 3. What changed and why

Each item is marked C (committed), U (a committed file with uncommitted edits)
or N (a new file that is not tracked by git yet).

### 3.1 Login and session (C, with U refinements)

New Django app `accounts`, ported from the legacy `service-infrastructure`
views and trimmed to what an API service needs.

| Path | What it does |
| --- | --- |
| `/csrfform` | Hands out a CSRF token in the exact HTML form the deployed SDK scrapes |
| `/account/login` | Validates a password or the SDK's signed-timestamp credential and opens a session |
| `/account/whoAmI` | Who the session belongs to |
| `/account/switch` | Moves the session to another tenant or identity |
| `/loginswitchbypass` | Login and switch in one call, used when the SDK is given a tenant at login |
| `/account/keys` | Registers the public key that SDK sign-in uses |
| `/account/logout` | Ends the session |

Design points:

- **The session lives in a signed cookie** (`SESSION_ENGINE` in
  `aviso_core/settings.py`). Several tasks run behind one load balancer and
  none owns a session store. A session therefore cannot be revoked on the
  server; it ends when the cookie expires or the user logs out.
- **Django has no database.** `DATABASES` uses the dummy backend and the admin
  app is removed. `accounts.User` exists only to satisfy `AUTH_USER_MODEL`;
  saving it raises. Users are read through the `aviso` library.
- **Only the administrative domain gets a session.** A tenant user is refused
  at login; services acting for a tenant use an `Access-Token` instead.
- `accounts/apps.py` disconnects Django's "update last login" signal. It was
  saving the user, which triggered a welcome mail whose template does not
  exist, so the first login of every user returned a 500.
- (U) `get_timezone` refuses to read another tenant's config while running as
  a different tenant, and `Switch` reads the new tenant's timezone inside a
  context for that tenant. Tenant config is now read from the context tenant's
  stack, so reading it for another tenant would look in the wrong database.

### 3.2 How a request gets its tenant (C, with U refinements)

`accounts/context.py` decides who a request runs as, tried in this order:

1. **Session**: a logged-in SDK or browser user. The tenant comes from the
   session, never from a header.
2. **`Access-Token` header**: service-to-service. The token maps to one tenant
   through the `authorization_tokens` table.
3. **`Internal-Api-Key` header**: the legacy shared secret, with the tenant
   taken from `X-Tenant-Name`. Still supported; intended to be removed once the
   remaining callers move off it.

Each caller's tenant comes from its own source only, and they never mix: a
session caller's `X-Tenant-Name` and `?tenant_name=` are ignored, and a key
caller must name its tenant with `X-Tenant-Name` or `?tenant_name=`. There is
no default tenant (a key request naming neither used to run against
`aviso.com`; now it gets a 401). Views read the tenant from `sec_context`,
never from the request: `basic_results` and `hier_sync` used to read the
header themselves, which failed session callers and let them name another
tenant.

`aviso_core/middlewares.py` calls this for every path except a short list of
anonymous ones (`/csrfform`, `/account/login`, `/account/logout`,
`/loginswitchbypass`, `/sdk/version`, `/sdk/latest`, `/account/validate_ip`),
and returns 401 otherwise. After the response it closes every connection the
request opened; for a streaming response it does so when the stream ends.

- (U) A bare token names no tenant, so `_find_token` asks each configured stack
  database in turn (`<STACK>_PG_DB_CONNECTION_URL`). The result is cached per
  process for `TOKEN_CACHE_TTL` seconds, 60 by default. That cache is the
  revocation window.
- (U) `_release()` also closes the per-request stack connection added in
  aviso-infrastructure. It reads the thread-local attributes directly, because
  the properties create a connection when none exists.
- (U) `gbm_apis/framework/baseView.py`: the lockout check at login and both
  token checks read from the tenant's stack database. `AvisoView` now passes a
  view's own `HttpResponse` straight through; before, a deliberate 403 or 404
  returned by a view fell into `json.dumps` and left as a 500.

### 3.3 The SDK package (C)

New Django app `sdk`.

- `python manage.py preparesdk` zips `gbm_apis/sdk/` and
  `gbm_apis/interface/` into the layout `avisosdk` downloads and imports. The
  version is a hash of the packaged bytes, so every task agrees on it without
  coordination and it does not depend on git being present in the image.
- `/sdk/version` and the `SDK_VERSION` response header tell the client which
  package to fetch. The package is served from `/static/<version>.zip` by
  WhiteNoise, or from `/sdk/latest` when the client runs with `sdk_debug` on.
- `docker-entrypoint.sh` runs `preparesdk` before gunicorn starts, and fails
  the task if it fails. `/gbm/health` would otherwise answer 200 while no SDK
  client could connect.

Everything under `gbm_apis/sdk/` and `gbm_apis/interface/` runs **on the
client**, inside that package. It must not import Django or `aviso.settings`,
must use relative imports, and must import under Python 2 as well as 3, since
SDK clients still run Python 2. `gbm_apis/interface/relativedelta.py` is
vendored because `python-dateutil` is not a dependency of SDK clients.

### 3.4 Shell commands and the endpoints behind them

Shell commands registered in `gbm_apis/sdk/shell.py`: `timezone`, `tenant`,
`epoch` and the date conversions, `current_period`, `prev_period`,
`next_period`, `dataset`, `uipmeta`, `sourcemeta`, `sandbox`.

| Area | Server side | Client side | State |
| --- | --- | --- | --- |
| Dataset read and update | `gbm_apis/dataset/datasetdesigner.py` at `/gbm/dataset/...` | `gbm_apis/sdk/data.py` | C |
| Period boundaries | `gbm_apis/api/date_manager.py` at `/gbm/date/current`, `/next`, `/prev` | `gbm_apis/interface/shellDateUtils.py` | N server, U client |
| Tenant config | `gbm_apis/api/tenant_config.py` at `/gbm/tenant/<name>/config[/<category>/<name>]` | `tenant()` in `shell.py` | N server, U client |
| Sandbox list and commit | `gbm_apis/api/sandbox_manager.py` at `/gbm/sandbox/list`, `/commit` | `gbm_apis/sdk/sandbox.py` | N both |
| Authorization tokens | `gbm_apis/api/auth_tokens.py` at `/set_auth_token`, `/get_auth_token` (root, not under `/gbm/`) | called with `shell.api` | N |

Notes on these:

- The three period commands are registered together because the client builds
  its period cache from current, previous and next in one pass, so
  `current_period()` alone still calls the other two endpoints.
- `tenant('get_config', category='forecast', config_name='timezone')` is
  answered from the shell without a server call, because the SDK asks for it
  during login before any tenant is selected.
- The dataset endpoints read through the library's `DSClass`, the same class
  the legacy service used, not through this repository's own `Dataset` fork.
  The comment at the top of `gbm_apis/dataset/datasetdesigner.py` explains what
  the fork got wrong for this purpose.
- Tenant flags: `tenant('get_flag', ...)` and `tenant('set_flag', ...)` reach
  `GET`/`POST /gbm/tenant/<name>/flag/<category>/<config_name>`, legacy's
  `TenantFlag`. Flags live in `tenant_flags`, apart from config
  (`tenant_configs`), are not sandboxed (a `POST` is live at once), and are
  read and written in the request tenant's own GBM stack database.
- **Deliberately not ported**, with the reason recorded in each module's
  docstring: dataset `list` and `indexes`; sandbox `purge`, `resync`,
  `clear_data`, `list_stale`; tenant user management, `bulk_config`,
  `validate_config`; date `shift`; token listing and removal.
- `get_auth_token` returns the tenant's stack as `stack`, where the legacy
  service returned its `CNAME`. Whether those are the same string in production
  has not been compared yet.

### 3.5 `deals_results` and `basic_results` crash fix (U)

Both endpoints failed for every tenant on this branch with
`'NoneType' object has no attribute 'get'`.

Cause: `get_from_micro` in `gbm_apis/domainmodel/datameta.py` and
`gbm_apis/deal_result/dataset.py` read
`sec_context.get_microservice_config('gbm_service').get('sandbox')` on its
first lines. With the new `aviso` wheel the tenant record comes from the
tenant's stack database, and that record has no `gbm_service` entry (see the
aviso-infrastructure handover, section 3.1). Master does not fail because its
older wheel reads the tenant record from FM, where the entry exists.

Fix: the two `sandbox` lookups moved into the branch that uses them (the
fetch-over-shell path) and tolerate a missing entry. The path both endpoints
actually take, `get_from_db=True` for `OppDS`, reads the dataset config
straight from the ETL and GBM stack databases and never needed either value.

### 3.6 Image and startup (partly C, mostly U)

Committed: the entrypoint that runs `preparesdk`, the `ENTRYPOINT` line, and
WhiteNoise. Everything about git and the config repositories is uncommitted.

- `Dockerfile`: installs `git` and `ca-certificates`; sets the git author and
  committer identity as environment variables; sets `CONFIG_REPO` to a folder
  owned by the non-root user; adds a small `GIT_ASKPASS` helper so the
  repository token is never written into `.git/config`; makes `/app/static`
  writable so the SDK package can be built; sets the entrypoint.
- `docker-entrypoint.sh`: after `preparesdk`, when `BACKUP_CONFIG=True`,
  clones one tenant-config repository per stack listed in `CONFIG_REPO_STACKS`
  into `$CONFIG_REPO/<stack>`. The URL for a stack comes from
  `<STACK>_CONFIG_REPO_URL` (`gbm1-ms` gives `GBM1_MS_CONFIG_REPO_URL`), the
  branch from `CONFIG_REPO_BRANCH` (default `master`). A missing variable or a
  failed clone stops the task on purpose. `BACKUP_CONFIG` is the only switch,
  in every mode, and it is the same one aviso's `fileUtils` checks before a
  backup. It is on only for `BACKUP_CONFIG=True` exactly: the entrypoint's
  `is_backup_on` and aviso's `is_backup_on()` apply the same rule, so the clone
  and the backups never disagree. Anything else, unset included, is off; an
  unexpected value such as `true` logs a warning and the server still starts.
  With it off the task needs no repository, token or GitHub access, config
  saves skip the backup without an error, and a production task logs a warning
  at startup.
- `.dockerignore` excludes `.env`; `.gitignore` excludes the built SDK package.
- `requirements.txt`: `aviso==0.1.13` and `pyschema` removed (U); `whitenoise`
  added (C). The last commit still pins `aviso==0.1.15`.
- `task-definitions/qa/aviso-core-qa-td.json` (C): QA runs gunicorn with 3
  workers; the image default of 33 failed there.
- `aviso_core/wsgi.py`, `manage.py`, `sdk/tests.py`: comments marking the
  Python-2 `pyschema` import shims as dead code to remove once a deployed run
  confirms nothing else relies on the `zipimport` patch next to them.

Environment variables to add before this image is deployed (names only):

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | **Required.** Django's secret key, which signs the session cookies. Same value that used to be hard-coded in `aviso_core/settings.py`. Put it in the S3 `.env` **unquoted** (ECS keeps quotes as part of the value); the local `.env` has it single-quoted, which dotenv and docker compose both strip. Without it the task exits at startup |
| `BACKUP_CONFIG` | `True` turns the tenant-config git backup on; anything else, unset included, leaves it off (an unexpected value logs a warning). The four `CONFIG_REPO_*` variables below are needed only when it is `True` |
| `CONFIG_REPO_STACKS` | Comma-separated stacks to back up, for example `gbm1-ms,gbm2-ms` |
| `<STACK>_CONFIG_REPO_URL` | One per listed stack |
| `CONFIG_REPO_TOKEN` | Read access and push access to those repositories |
| `CONFIG_REPO_BRANCH` | Optional; defaults to `master` |
| `GBM1_MS_PG_DB_CONNECTION_URL`, `GBM2_MS_PG_DB_CONNECTION_URL` | Now required for tenants on each stack, with no fallback |
| `TOKEN_CACHE_TTL` | Optional; seconds an `Access-Token` lookup is cached |

With `BACKUP_CONFIG=True`, a task without `CONFIG_REPO_STACKS`, a stack's URL
or a reachable repository exits at startup, so set those before switching it
on. `mode=production` no longer turns the backup on by itself.

### 3.7 Tests and docs (C, with U and N additions)

- `accounts/tests/test_login_contract.py` (18 tests): each assertion mirrors
  something the deployed SDK does. Databases are mocked.
- `accounts/tests/test_sdk_end_to_end.py` (4 tests): drives the real `avisosdk`
  against a live test server.
- `accounts/tests/test_authentication_database.py` (N, 3 tests): the user
  lookup and update during login use the authentication database scope.
- `sdk/tests.py` (9 tests): the package layout and version contract.
- `aviso_core/test_settings.py`: settings for these, with an in-memory sqlite.
- `docs/sdk-usage.ipynb`: how to call the three GBM APIs through the SDK.
- `docs/baselines/dev-quest_rts-before.json` and `scripts/api_fingerprint.sh`:
  a way to record what an environment answers before and after a deploy as
  field names and a hash, without storing customer values.

## 4. How to run and check it

Run locally without Docker. Django's development server has no request timeout:

```
source .venv/bin/activate
python manage.py preparesdk
python manage.py runserver 127.0.0.1:8000 --noreload
```

With Docker, the image must be rebuilt for any code change, because
`docker-compose.yml` has no volume mount and no build section:

```
docker build -t aviso-core .
docker compose up -d --force-recreate
```

`docker-compose.yml` overrides the image's command with
`gunicorn ... --reload` and no `--timeout`, so gunicorn's 30-second default
applies and it kills the worker on slow requests (`WORKER TIMEOUT` in the
logs). The Dockerfile's own command uses `--timeout 1200`. Add `--timeout 1200`
to the compose command before relying on Docker locally. This has not been
changed.

Check scripts, kept outside this repository in
`/Users/work/Documents/gbm/scripts/`. All of them only send GET requests.

| Script | What it proves |
| --- | --- |
| `gbm_readonly_checks.py` | Logs in to local and to a production GBM server with the SDK and compares whoAmI, tenant config reads, period boundaries, dataset read, sandbox list and `get_auth_token` (the token is compared as a hash) |
| `gbm_results_api_checks.py` | Compares `deals_results` and `basic_results` on local against production GBM and production aviso-core; passes if local matches at least one |
| `gbm_debug_local_results.py` | Calls the two functions behind those endpoints directly and prints the full traceback, which the streaming endpoint does not log |
| `gbm_prod_core_check.py` | Calls the two endpoints on production aviso-core with the internal API key |

The results comparison ignores three things that differ between any two server
processes and are not data differences: NaN on both sides, the order of the
`__segs` list, and the order of names in `" / "`-joined text such as
`as_of_OwnerSplits`. The settings are constants at the top of the script.

## 5. Verified and not verified

Verified on 2026-11-15, local development server against production data:

- ivision.com (`gbm1-ms`), 2026Q4: `deals_results` identical to production GBM
  and to production aviso-core after the fix in 3.5.
- ivision.com: `gbm_readonly_checks.py` passed 15 of 15 against production GBM.
  That run was before the `get_auth_token` check was added.
- Production aviso-core, which runs master, returns `deals_results` for
  ivision.com. The crash in 3.5 is specific to this branch's wheel.
- The `aviso` package in `.venv`, the wheel in `wheels/` and the copy inside
  the Docker container running at the time were identical file for file.

Not verified:

- **`basic_results` data.** Every run so far asked for the last 24 hours over a
  weekend and all three servers answered "No records returned". The endpoint no
  longer crashes, but its records have not been compared. Pin a weekday
  `from_timestamp` in the script's `CASES`.
- **certinia.com (`gbm2-ms`), 2027Q4.** Local returned the deals. The
  comparison then failed on owner-name ordering only, as far as the first 50
  listed differences show; the script was changed to ignore that, and the run
  has not been repeated.
- **The new `get_auth_token` check** in `gbm_readonly_checks.py` has not been
  run.
- **Any write**: `set_config`, sandbox commit, dataset update, `set_auth_token`,
  key registration. Only reads were exercised.
- **The `Access-Token` path** end to end against real stack databases.
- **The test suite.** It was not run for this handover.
- **The image in a deployed environment**: the config repository clones, the
  git backup, and `/gbm/health` after the entrypoint change.
- **Tenants other than those two.**

## 6. Open issues and risks

- **Local runs act on production.** The local `.env` sets `mode=production`
  and points at production databases, and the entrypoint clones the real config
  repositories (on a test branch locally). A config write from a local run
  would change production data and push a backup commit.
- **Local slowness is unexplained.** `deals_results` took 98 seconds for
  ivision.com and 377 seconds for certinia.com locally, against 12 to 18
  seconds on both production servers; `basic_results` took about 30 seconds to
  answer "no records". Network distance from a laptop explains some of it, but
  nobody has traced where the time goes. Check this before trusting the branch
  under a request timeout.
- **A streaming endpoint that fails after it starts looks like a success.**
  `deals_results` sends HTTP 200 and the first bytes, then the generator
  raises; the client sees a cut connection and no traceback is logged. This is
  how the crash in 3.5 first appeared (12 bytes received).
- **The cutover may break stack resolution.** `docs/sdk-usage.ipynb` describes
  the intended switch as pointing a tenant's `microservices_info.gbm_service.host`
  in FM at aviso-core. The `aviso` library derives a tenant's stack from the
  first label of that same host name. If the host becomes aviso-core's, the
  stack name would no longer be `gbm1-ms` or `gbm2-ms`. I have not checked how
  this is meant to work; settle it before any tenant is switched.
- **`get_oppds_config` in `utils/db_read_utils.py` takes the first `OppDS` row**
  it finds for a tenant, with no sandbox filter and no ordering. Whether a
  tenant can have more than one such row was not checked. This code is on
  master too.
- **Shell-path dataset lookups log an exception for stack tenants.** For any
  dataset other than `OppDS`, `get_from_micro` now reaches its "found
  etl_data_service but not gbm_service" check, logs, and falls back to the
  tenant's own config. It works but is noisy.
- **A session cannot be revoked server side**, and a revoked `Access-Token`
  keeps working for up to `TOKEN_CACHE_TTL` seconds per worker.
- **`Internal-Api-Key` is still accepted.**

## 7. Decisions made on purpose

Read these before "fixing" any of them.

- **Signed-cookie sessions, no session store, no Django database.** Chosen so
  that any task can serve any request.
- **Sessions only for the administrative domain.**
- **SDK version is a content hash, not a git revision.**
- **`preparesdk` and the config repository clone are fatal at startup.** A task
  that cannot serve the SDK or back up tenant config should not take traffic
  while looking healthy.
- **The repository token goes through `GIT_ASKPASS`**, not into the remote URL.
- **The `gbm_service` lookup was moved, not papered over with data.** Adding a
  `gbm_service` entry to every tenant's stack record would also have stopped
  the crash, but the legacy service never had it and the code path that needs
  it is not the one these endpoints use.
- **The welcome-mail signal is disconnected** instead of shipping the template.
  User lifecycle belongs to the main application, not to this service.
- **Endpoints not ported are listed in each module's docstring.** The SDK
  modules register only the commands the server can answer.

## 8. For an AI agent picking this up

- Start with section 1 and run the three commands there. Check `git status`
  before assuming a file is committed; much of this branch is not.
- Read `accounts/context.py`, `aviso_core/middlewares.py` and
  `gbm_apis/sdk/shell.py` first. Their comments carry the reasoning; where a
  comment and this document disagree, trust the code and say so.
- Code under `gbm_apis/sdk/` and `gbm_apis/interface/` runs on the client. Keep
  it free of Django and `aviso.settings`, keep imports relative, and keep it
  importable under Python 2.
- The local environment reaches production. Do not run write paths, and do not
  send requests to production services, unless the user asks for exactly that.
- The user wants a diagnosis agreed before any edit: explain the cause, propose
  the change, and edit only after they say so. Use reviewable edits rather than
  scripted bulk changes.
- Keep exactly one `aviso` wheel in `wheels/`. After a wheel changes, the
  Docker image must be rebuilt and a virtualenv needs
  `pip install --force-reinstall --no-deps`; verify by comparing file contents,
  not version numbers.
- Do not print or write secrets: no values from `.env`, tokens, passwords or
  connection strings in output, commits or documents.
