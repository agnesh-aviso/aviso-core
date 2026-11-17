"""Tests for clear_sandbox_data, reached through POST /gbm/sandbox/commit with
clear_sandbox_data=true (the SDK's commit(drop_sandbox_data=True)), and for the
aviso GnanaMongoDB listing it relies on.

No database and no network: gnana_db, Sandbox and sec_context are replaced with
fakes. Collection names follow the real builders:

    <tenant>.<ds>._uip.<sbx>                         InboxEntryClass, gbm_apis/domainmodel/datameta.py
    <tenant>.<ds>._results.<model>.<sbx>[.<quarter>] gbm_apis/deal_result/dataset.py (quarter: existingpipeforecast only)
    <tenant>.<ds>._runs.<model>.<sbx>                gbm_apis/deal_result/dataset.py
    <tenant>.combined_results.<target>.<key>.<sbx>   gbm_apis/forecast_result/combined_results.py

Live data uses the reserved names in the sandbox's place (_data, _result, _run)
or has no sandbox part at all (combined_results.<target>.main).

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_sandbox_clear.py -q -p no:cacheprovider
"""
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
from aviso.framework.mongodb import GnanaMongoDB  # noqa: E402

import gbm_apis.api.sandbox_manager as sandbox_manager  # noqa: E402

TENANT = "acme.com"
SANDBOX = "sbx1"

# Sandbox sbx1's own collections: these, and only these, are dropped.
SBX1_COLLECTIONS = [
    "acme.com.opps._uip.sbx1",
    "acme.com.opps._results.bookings_rtfm.sbx1",
    "acme.com.opps._results.existingpipeforecast.sbx1.2026Q4",
    "acme.com.opps._runs.bookings_rtfm.sbx1",
    "acme.com.combined_results.bookings.main.sbx1",
]

LIVE_COLLECTIONS = [
    "acme.com.opps._uip._data",
    "acme.com.opps._results.bookings_rtfm._result",
    "acme.com.opps._results.existingpipeforecast._result.2026Q4",
    "acme.com.opps._runs.bookings_rtfm._run",
    "acme.com.combined_results.bookings.main",
    "acme.com.combined_results.bookings.expected",
]

OTHER_SANDBOX_COLLECTIONS = [
    "acme.com.opps._uip.sbx10",
    "acme.com.opps._results.bookings_rtfm.sbx10",
]

# The tenant's GBM database (one per tenant, so another tenant's collections
# would not be in it; one is included to cover the tenant prefix filter).
GBM_COLLECTIONS = (SBX1_COLLECTIONS + LIVE_COLLECTIONS + OTHER_SANDBOX_COLLECTIONS
                   + ["other.com.opps._uip.sbx1"])


class FakeGnanaDB(object):
    """gnana_db with the listing and the drop both on the GBM database."""

    def __init__(self):
        self.dropped = []
        self.db = types.SimpleNamespace(
            list_collection_names=lambda: list(GBM_COLLECTIONS))

    def collection_names(self, prefix):
        return (name for name in GBM_COLLECTIONS if name.startswith(prefix))

    def dropCollection(self, name):
        self.dropped.append(name)


@pytest.fixture
def env():
    fake_db = FakeGnanaDB()
    sandbox_model = mock.Mock()
    sandbox_model.getByName.side_effect = (
        lambda name: types.SimpleNamespace(id="sid-" + name))
    with mock.patch.object(sandbox_manager, "gnana_db", fake_db), \
            mock.patch.object(sandbox_manager, "Sandbox", sandbox_model), \
            mock.patch.object(sandbox_manager, "sec_context",
                              types.SimpleNamespace(name=TENANT)):
        yield fake_db, sandbox_model


def test_drops_exactly_the_sandbox_collections(env):
    fake_db, sandbox_model = env

    response = sandbox_manager.clear_sandbox_data(SANDBOX)

    assert response["success"] is True
    assert sorted(fake_db.dropped) == sorted(SBX1_COLLECTIONS)
    assert sorted(response["collections_dropped"]) == sorted(SBX1_COLLECTIONS)
    sandbox_model.remove.assert_called_once_with("sid-" + SANDBOX)


# Names that also appear inside live collection names: a dataset (opps), a
# target (bookings), a model (bookings_rtfm, existingpipeforecast), a quarter
# and a piece of the tenant name (com, acme). Legacy matched '.<sandbox>'
# anywhere in a _results name and dropped live results for these.
@pytest.mark.parametrize("sandbox_name", [
    "opps", "bookings", "bookings_rtfm", "existingpipeforecast", "2026Q4",
    "com", "acme",
])
def test_name_shared_with_live_data_drops_nothing_live(env, sandbox_name):
    fake_db, _ = env

    response = sandbox_manager.clear_sandbox_data(sandbox_name)

    assert response["success"] is True
    assert fake_db.dropped == []


def test_reserved_name_removes_the_record_but_drops_nothing(env):
    fake_db, sandbox_model = env

    response = sandbox_manager.clear_sandbox_data("main")

    assert response["success"] is False
    assert fake_db.dropped == []
    sandbox_model.remove.assert_called_once_with("sid-main")


def test_missing_sandbox_drops_nothing(env):
    fake_db, sandbox_model = env
    sandbox_model.getByName.side_effect = lambda name: None

    response = sandbox_manager.clear_sandbox_data(SANDBOX)

    assert response == {"success": False, "collections_dropped": {},
                        "reason": "sandox not available"}
    assert fake_db.dropped == []
    sandbox_model.remove.assert_not_called()


# --- aviso GnanaMongoDB (the installed wheel) --------------------------------
# In production these used to list sec_context.tenant_db -- the FM Mongo
# database -- and then read or drop through self.db, the GBM Mongo database.

class FakeMongoDatabase(object):
    def __init__(self, names):
        self.names = list(names)
        self.dropped = []

    def collection_names(self):
        return list(self.names)

    def __getitem__(self, name):
        dropped = self.dropped
        return types.SimpleNamespace(drop=lambda: dropped.append(name))


class ProductionContext(object):
    name = TENANT

    @property
    def tenant_db(self):
        raise AssertionError("listed the FM Mongo database (sec_context.tenant_db)")


def production_gnana_db(gbm):
    gnana_db = GnanaMongoDB()
    gnana_db.is_prod = True
    gnana_db.sec_context = ProductionContext()
    gnana_db.db = gbm
    return gnana_db


def test_collection_names_lists_gbm_in_production():
    gbm = FakeMongoDatabase(GBM_COLLECTIONS)

    listed = list(production_gnana_db(gbm).collection_names(TENANT))

    assert listed == [name for name in GBM_COLLECTIONS if name.startswith(TENANT)]


def test_drop_collections_in_namespace_lists_gbm_in_production():
    gbm = FakeMongoDatabase(GBM_COLLECTIONS)

    production_gnana_db(gbm).dropCollectionsInNamespace("acme.com.opps")

    assert sorted(gbm.dropped) == sorted(
        name for name in GBM_COLLECTIONS if name.startswith("acme.com.opps."))
