"""The tenant-config backup switch in aviso.utils.fileUtils (the installed wheel).

aviso.settings.is_backup_on() is the switch: on only for BACKUP_CONFIG=True,
exactly -- the same rule docker-entrypoint.sh clones the config repos under, so
the two can never disagree. Production (mode=production) used to force the
backup on, and is_true() used to read "true" or "1" as on too.

No git and no network: process_shell_command (which runs git) and the repo
location are replaced; files are written to a temporary directory.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_config_backup.py -q -p no:cacheprovider
"""
import contextlib
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
import aviso.utils.fileUtils as fileUtils  # noqa: E402
from aviso.utils import GnanaError  # noqa: E402

TENANT = "acme.com"
FILE = "tenantconfig_acme.com.json"


@pytest.fixture
def repo(tmp_path):
    """A config repo clone that records the git commands run in it."""
    git = []

    def run_git(path, command_list, command):
        git.append(command_list[:2])
        if command == "push" and repo.fail_push:
            raise GnanaError("command:push :error:rejected")

    repo = types.SimpleNamespace(path=tmp_path, git=git, fail_push=False)
    with mock.patch.object(fileUtils, "process_shell_command", side_effect=run_git), \
            mock.patch.object(fileUtils, "_config_repo_path", return_value=str(tmp_path)), \
            mock.patch.object(fileUtils, "sec_context", types.SimpleNamespace(name=TENANT)):
        yield repo


@contextlib.contextmanager
def production_with(backup_config):
    """A production deployment with BACKUP_CONFIG set as given (None: unset)."""
    env = {k: v for k, v in os.environ.items() if k != "BACKUP_CONFIG"}
    if backup_config is not None:
        env["BACKUP_CONFIG"] = backup_config
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(fileUtils, "ISPROD", True, create=True):
        # ISPROD is gone from fileUtils; created here to show it no longer counts.
        yield


@pytest.mark.parametrize("value", [None, "", "False", "true", "TRUE", "1", "yes"])
def test_anything_but_True_leaves_backup_off(repo, value):
    with production_with(value):
        status, errors = fileUtils.gitbackup_dataset(FILE, {"a": 1}, "change")

    assert (status, errors) == (True, None)
    assert repo.git == []
    assert not (repo.path / TENANT).exists()


def test_True_commits_and_pushes(repo):
    with production_with("True"):
        status, errors = fileUtils.gitbackup_dataset(FILE, {"a": 1}, "change")

    assert (status, errors) == (True, None)
    assert repo.git == [["git", "pull"], ["git", "add"], ["git", "commit"], ["git", "push"]]
    assert (repo.path / TENANT / FILE).read_text().strip() == "{'a': 1}"


def test_failed_push_is_reported_not_raised(repo):
    repo.fail_push = True
    with production_with("True"):
        status, errors = fileUtils.gitbackup_dataset(FILE, {"a": 1}, "change")

    assert status is False
    assert "Problem in updating config repository" in errors


def test_reading_from_the_repo_follows_the_same_switch(repo):
    with production_with("False"):
        assert fileUtils.get_git_content(FILE) == {}
    assert repo.git == []
