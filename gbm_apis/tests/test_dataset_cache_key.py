"""The dataset config cache key aviso-core writes after a config change.

aviso.domainmodel.sandbox.recache_dataset_config (installed wheel) writes each
dataset's config to the global Redis cache for every service registered with
this tenant -- in practice the main app -- under get_global_cache_key(). The
main app (analytics-server, domainmodel/datameta.py Dataset.get_from_micro)
reads the GBM half of a dataset's config under a key ending in "gbm", and uses
it whenever it is there, with no expiry.

Legacy gbm-service ran with AVISO_APPS=gbm and so wrote that key. aviso-core
runs with AVISO_APPS=aviso_core; written under that name the key was never
read, and the app kept the GBM config it had cached before the change.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_dataset_cache_key.py -q -p no:cacheprovider
"""
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
import aviso.domainmodel.sandbox as sandbox  # noqa: E402

# The main app's token, as onboarding registers it with the GBM service: its
# stack is the app's CNAME, so the key starts with what the app reads under.
APP_TOKEN = types.SimpleNamespace(stack="app", tenant="acme.com")


def key_the_app_reads(cname, tenant, dataset, stage, target, full_config, source):
    """analytics-server Dataset.get_from_micro's etl_key / gbm_key."""
    return ":".join([cname, tenant, dataset, stage if stage else "None", target,
                     str(full_config), source])


@pytest.mark.parametrize("stage", [None, "sbx1"])
@pytest.mark.parametrize("full_config", [True, False])
def test_aviso_core_writes_the_gbm_key_the_app_reads(stage, full_config):
    with mock.patch.object(sandbox, "AVISO_APPS", "aviso_core"):
        written = sandbox.get_global_cache_key(APP_TOKEN, "OppDS", stage, full_config)

    assert written == key_the_app_reads("app", "acme.com", "OppDS", stage,
                                        "_data", full_config, "gbm")


@pytest.mark.parametrize("apps", ["etl", "gbm"])
def test_other_services_keep_their_own_ending(apps):
    with mock.patch.object(sandbox, "AVISO_APPS", apps):
        written = sandbox.get_global_cache_key(APP_TOKEN, "OppDS", "sbx1", False)

    assert written == "app:acme.com:OppDS:sbx1:_data:False:%s" % apps
