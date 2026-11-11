"""Tests for ``GET /gbm/hier_metadata``. No database and no network: Mongo is faked.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_hier_metadata.py -q -p no:cacheprovider
"""
import hashlib
import json
import os
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import bson  # noqa: E402
import pytest  # noqa: E402
from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.urls import Resolver404, resolve  # noqa: E402

from aviso.framework import tenant_holder  # noqa: E402
from aviso.framework.connection_factory import ConnectionFactory  # noqa: E402
from aviso.settings import sec_context  # noqa: E402

import gbm_apis.hierarchy_sync.sync_service as sync_service  # noqa: E402
from gbm_apis.api.hier_metadata import HierarchyMetadataAPIView  # noqa: E402
from gbm_apis.hierarchy_sync.sync_service import HIER_COLL, get_hier_metadata  # noqa: E402

TENANT = "acme.com"

# Natural (insertion) order is deliberately NOT sorted by node, and key order differs between documents.
HIER_DOCS = [
    {"_id": bson.ObjectId(), "node": "Global#B", "parent": "Global#!", "from": 0, "to": None, "hidden": False,
     "how": "hier_svc", "label": "Zürich – 東京 ✓"},
    {"_id": bson.ObjectId(), "parent": None, "node": "Global#!", "to": None, "from": 0, "hidden": False,
     "label": "Global"},
    {"_id": bson.ObjectId(), "node": "Global#A", "parent": "Global#!", "from": 1514764800, "to": 2 ** 40,
     "hidden": True, "weight": 0.1, "meta": {"z": [1, 2.5, None], "a": {"b": "c"}}},
]


@pytest.fixture(autouse=True)
def tenant_context():
    tenant_holder.set_context(user_name="test", tenant_name=TENANT, login_tenant_name=TENANT)
    yield


@pytest.fixture(autouse=True)
def no_fm_tenant_db():
    """aviso-core's ``sec_context.tenant_db`` is the FM (app) Mongo DB: hier_metadata must never read it."""
    with mock.patch.object(type(sec_context), "tenant_db", new_callable=mock.PropertyMock,
                           side_effect=AssertionError("sec_context.tenant_db (FM Mongo) was used")):
        yield


class _ReadOnlyCollection:
    """Supports ``find`` only; any other attribute (insert/update/delete/drop/index/...) fails the test."""

    def __init__(self, store, name, finds):
        self._store, self._name, self._finds = store, name, finds

    def find(self, *args, **kwargs):
        self._finds.append((self._name, args, kwargs))
        filt, projection = (args + (None, None))[:2]
        assert filt == {}, filt
        out = []
        for raw in self._store.get(self._name, []):
            doc = bson.BSON(raw).decode()               # same decoding (types, key order) as pymongo
            if projection == {"_id": 0}:
                doc.pop("_id", None)
            out.append(doc)
        return iter(out)

    def __getattr__(self, attr):
        raise AssertionError("hier_metadata used Collection.%s (only find is allowed)" % attr)


class _ReadOnlyDb:
    def __init__(self, store, finds):
        self._store, self._finds = store, finds

    def __getitem__(self, name):
        return _ReadOnlyCollection(self._store, name, self._finds)

    def __getattr__(self, attr):
        raise AssertionError("hier_metadata used Database.%s (only db[coll].find is allowed)" % attr)


@pytest.fixture
def gbm_mongo():
    """Patch ConnectionFactory.get_mongo_db; yields (store, finds, db_calls). Store docs as BSON bytes."""
    store, finds, db_calls = {}, [], []

    def get_mongo_db(tenant, db_type="etl", cname="preprod"):
        db_calls.append((tenant, db_type, cname))
        return _ReadOnlyDb(store, finds)

    with mock.patch.object(ConnectionFactory, "get_mongo_db", side_effect=get_mongo_db):
        yield store, finds, db_calls


def _put(store, docs, coll=HIER_COLL):
    store[coll] = [bson.BSON.encode(d) for d in docs]


def _legacy_checksum(docs):
    """Legacy formula, written out: sha1 of json.dumps of the docs without _id, in natural order."""
    recs = [{k: v for k, v in d.items() if k != "_id"} for d in docs]
    return hashlib.sha1(bytes(json.dumps(recs), "utf-8")).hexdigest()


def _get(path="/gbm/hier_metadata"):
    request = RequestFactory().get(path)
    request.user = AnonymousUser()
    return HierarchyMetadataAPIView.as_view()(request)


# ============================================================================= URL
@pytest.mark.parametrize("path", ["/gbm/hier_metadata", "/gbm/hier_metadata/"])
def test_url_resolves_with_and_without_trailing_slash(path):
    assert resolve(path).func.view_class is HierarchyMetadataAPIView


@pytest.mark.parametrize("path", ["/gbm/hier_metadatax", "/gbm/hier_metadata/x", "/gbm/xhier_metadata"])
def test_lookalikes_are_not_routed(path):
    with pytest.raises(Resolver404):
        resolve(path)


@pytest.mark.parametrize("path", ["/gbm/hier_sync", "/gbm/hier_sync/"])
def test_hier_sync_is_a_separate_view(path):
    assert resolve(path).func.view_class is not HierarchyMetadataAPIView


# ============================================================================= service
def test_reads_gbm_db_hierarchy_with_the_exact_legacy_query(gbm_mongo, monkeypatch):
    store, finds, db_calls = gbm_mongo
    monkeypatch.delenv("CNAME", raising=False)
    _put(store, HIER_DOCS)
    _put(store, [{"node": "other"}], coll="hierarchy_backup")

    assert get_hier_metadata() == _legacy_checksum(HIER_DOCS)
    assert db_calls == [(TENANT, "gbm", "preprod")], "must read the tenant's GBM DB (not FM)"
    assert finds == [(HIER_COLL, ({}, {"_id": 0}), {})], "no sort, no extra cursor options"


def test_uses_cname_from_environment(gbm_mongo, monkeypatch):
    _, _, db_calls = gbm_mongo
    monkeypatch.setenv("CNAME", "app")
    get_hier_metadata()
    assert db_calls == [(TENANT, "gbm", "app")]


def test_keeps_natural_order_and_key_order(gbm_mongo):
    store, _, _ = gbm_mongo
    _put(store, HIER_DOCS)
    by_node = sorted(HIER_DOCS, key=lambda d: d["node"])
    sorted_keys = [dict(sorted(d.items())) for d in HIER_DOCS]
    got = get_hier_metadata()
    assert got == _legacy_checksum(HIER_DOCS)
    assert got != _legacy_checksum(by_node), "documents must not be sorted"
    assert got != _legacy_checksum(sorted_keys), "keys must not be sorted"


def test_empty_or_missing_collection_is_checksum_of_empty_list(gbm_mongo):
    assert get_hier_metadata() == hashlib.sha1(b"[]").hexdigest()


def test_checksum_matches_sdk_formula(gbm_mongo):
    # aviso-data-sdk create_collection_checksum: sha1(json.dumps(list(find({}, {'_id': 0}))).encode('utf-8'))
    store, _, _ = gbm_mongo
    _put(store, HIER_DOCS)
    sdk = hashlib.sha1(json.dumps([{k: v for k, v in d.items() if k != "_id"} for d in HIER_DOCS])
                       .encode("utf-8")).hexdigest()
    assert get_hier_metadata() == sdk


def test_explicit_db_argument_is_used_like_legacy(gbm_mongo):
    _, _, db_calls = gbm_mongo
    store, finds = {}, []
    _put(store, HIER_DOCS[:1])
    assert get_hier_metadata(_ReadOnlyDb(store, finds)) == _legacy_checksum(HIER_DOCS[:1])
    assert db_calls == [] and len(finds) == 1


def test_logs_time_like_legacy(gbm_mongo):
    # the gnana.* loggers do not propagate to the root logger, so assert on the module logger itself
    assert sync_service.logger.name == "gnana.gbm_apis.hierarchy_sync.sync_service"
    with mock.patch.object(sync_service.logger, "warning") as warning:
        get_hier_metadata()
    (msg,), _ = warning.call_args
    assert warning.call_count == 1 and msg.startswith("Hier Meta Time: ")


# ============================================================================= view
@pytest.mark.parametrize("path", ["/gbm/hier_metadata", "/gbm/hier_metadata/"])
def test_view_returns_the_legacy_body_as_json(gbm_mongo, path):
    store, _, _ = gbm_mongo
    _put(store, HIER_DOCS)
    response = _get(path)
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    body = response.content.decode()
    assert body == json.dumps(_legacy_checksum(HIER_DOCS)), "legacy body: a JSON string, quoted hex"
    parsed = json.loads(body)
    assert isinstance(parsed, str) and len(parsed) == 40 and int(parsed, 16) >= 0


def test_view_ignores_query_parameters_like_legacy(gbm_mongo):
    store, _, _ = gbm_mongo
    _put(store, HIER_DOCS)
    assert _get("/gbm/hier_metadata?node=Global%23A&period=2026Q4").content == _get().content


def test_view_is_get_only_like_legacy(gbm_mongo):
    _, finds, db_calls = gbm_mongo
    request = RequestFactory().post("/gbm/hier_metadata", data=json.dumps([{"node": "x"}]),
                                    content_type="application/json")
    request.user = AnonymousUser()
    response = HierarchyMetadataAPIView.as_view()(request)
    assert response.status_code == 405 and response["Allow"] == "GET"
    assert db_calls == [] and finds == [], "a refused POST must not touch the database"
