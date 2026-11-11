"""Tests for ``POST /gbm/hier_sync``. No database and no network.

The GBM Mongo DB is an in-memory fake that follows pymongo 3.12's insert path (``InsertOne`` ->
``add_insert`` -> BSON encoding, unordered bulk writes, the unique index). Every test also blocks, and
fails on, any real connection: sockets, ``MongoClient``, Postgres, ``ConnectionFactory.resolve``, and
the FM (app) tenant DB, which holds the source-of-truth hierarchy and must never be used here.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_hier_sync.py -q -p no:cacheprovider
"""
import copy
import hashlib
import json
import os
import socket
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import bson  # noqa: E402
import psycopg2  # noqa: E402
import pytest  # noqa: E402
from bson.codec_options import CodecOptions  # noqa: E402
from bson.raw_bson import RawBSONDocument  # noqa: E402
from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.test import Client, RequestFactory, override_settings  # noqa: E402
from django.urls import Resolver404, resolve  # noqa: E402
from pymongo import InsertOne  # noqa: E402
from pymongo.common import validate_is_document_type  # noqa: E402
from pymongo.errors import BulkWriteError, InvalidOperation, ServerSelectionTimeoutError  # noqa: E402
from pymongo.mongo_client import MongoClient  # noqa: E402

from aviso.framework import tenant_holder  # noqa: E402
from aviso.framework.connection_factory import ConnectionFactory  # noqa: E402
from aviso.settings import sec_context  # noqa: E402

import aviso_core.middlewares as middlewares  # noqa: E402
import gbm_apis.hierarchy_sync.sync_service as sync_service  # noqa: E402
from gbm_apis.api.hier_sync import HierarchySyncAPIView  # noqa: E402
from gbm_apis.framework.baseView import protect_post  # noqa: E402
from gbm_apis.hierarchy_sync.sync_service import (  # noqa: E402
    HIER_COLL, InvalidHierarchyPayload, iter_chunks, sync_hierarchy, validate_hier_records)

TENANT = "acme.com"
API_KEY = "unit-test-key"
CNAME = "unit-cname"
CODEC = CodecOptions(unicode_decode_error_handler="ignore")   # as ConnectionFactory's (and legacy's) client

# Legacy gbm/services/sync_service.py hier_indexes, written out
LEGACY_INDEXES = [
    {"key": [("node", -1), ("parent", -1), ("from", -1), ("to", -1)], "unique": True},
    {"key": [("node", -1), ("hidden", -1), ("from", -1), ("to", -1)]},
    {"key": [("parent", -1), ("hidden", -1), ("from", -1), ("to", -1)]},
]


# ============================================================================= guards (every test)
VIOLATIONS = []


def _forbidden(what):
    def _raise(*args, **kwargs):
        VIOLATIONS.append(what)
        raise AssertionError("%s attempted during a hier_sync unit test" % what)
    return _raise


class _FmGuard:
    """Stands in for aviso-core's ``sec_context.tenant_db`` (the FM/app DB). The middleware's cleanup closes
    its client after every request (pre-existing behaviour); anything else is a violation."""

    def __getattr__(self, attr):
        if attr == "client":
            return types.SimpleNamespace(close=lambda: None)
        VIOLATIONS.append("FM tenant_db.%s" % attr)
        raise AssertionError("FM tenant_db.%s used by hier_sync" % attr)

    def __getitem__(self, name):
        VIOLATIONS.append("FM tenant_db[%r]" % name)
        raise AssertionError("FM tenant_db[%r] used by hier_sync" % name)


@pytest.fixture(autouse=True)
def no_real_connections():
    VIOLATIONS.clear()
    with mock.patch.object(socket.socket, "connect", _forbidden("socket.connect")), \
            mock.patch.object(socket, "create_connection", _forbidden("socket.create_connection")), \
            mock.patch.object(MongoClient, "__init__", _forbidden("MongoClient()")), \
            mock.patch.object(psycopg2, "connect", _forbidden("psycopg2.connect")), \
            mock.patch.object(ConnectionFactory, "resolve", _forbidden("ConnectionFactory.resolve")), \
            mock.patch.object(ConnectionFactory, "_get_pg_conn", _forbidden("ConnectionFactory._get_pg_conn")), \
            mock.patch.object(type(sec_context), "tenant_db", new_callable=mock.PropertyMock,
                              return_value=_FmGuard()), \
            mock.patch.object(type(sec_context), "postgres_local_con", new_callable=mock.PropertyMock,
                              return_value=None):
        # postgres_local_con: the middleware's cleanup reads it after every request (pre-existing), and the
        # property opens a Postgres connection (POSTGRES_DB_URL) so that it can be closed. Stubbed here.
        yield
    assert not VIOLATIONS, VIOLATIONS
    assert ConnectionFactory._client_cache == {}, "a real Mongo client was cached"


@pytest.fixture(autouse=True)
def tenant_context():
    tenant_holder.set_context(user_name="test", tenant_name=TENANT, login_tenant_name=TENANT)
    yield


# ============================================================================= fake GBM Mongo
class _BulkRecorder:
    """pymongo 3.12 ``_Bulk.add_insert``, which ``InsertOne._add_to_bulk`` calls."""

    def __init__(self):
        self.docs = []

    def add_insert(self, document):
        validate_is_document_type("document", document)
        if not (isinstance(document, RawBSONDocument) or "_id" in document):
            document["_id"] = bson.ObjectId()
        self.docs.append(document)


class _FakeGbmCollection:
    def __init__(self, db, name):
        self._db, self._name = db, name

    def create_indexes(self, indexes):
        docs = [dict(ix.document) for ix in indexes]
        self._db.ops.append(("create_indexes", self._name, docs))
        self._db.indexes[self._name] = docs

    def bulk_write(self, requests, ordered=True, bypass_document_validation=False, session=None):
        self._db.ops.append(("bulk_write", self._name, len(requests), ordered, bypass_document_validation))
        rec = _BulkRecorder()
        for r in requests:
            assert isinstance(r, InsertOne), r
            r._add_to_bulk(rec)
        if not rec.docs:
            raise InvalidOperation("No operations to execute")
        encoded = [bson.encode(d) for d in rec.docs]       # pymongo encodes with check_keys=False
        stored = self._db.store.setdefault(self._name, [])
        unique = self._db.indexes.get(self._name) and any(ix.get("unique") for ix in self._db.indexes[self._name])
        errors = []
        for i, (doc, raw) in enumerate(zip(rec.docs, encoded)):
            keys = [bson.encode({"k": doc["_id"]})]
            if unique:
                keys.append(bson.encode({"k": [doc.get(f) for f in ("node", "parent", "from", "to")]}))
            if any(k in self._db.seen for k in keys):
                errors.append({"index": i, "code": 11000, "errmsg": "E11000 duplicate key error", "op": doc})
                if ordered:
                    break
                continue
            self._db.seen.update(keys)
            stored.append(raw)
        if errors:
            raise BulkWriteError({"writeErrors": errors, "writeConcernErrors": [], "nInserted": len(stored),
                                  "nUpserted": 0, "nMatched": 0, "nModified": 0, "nRemoved": 0, "upserted": []})

    def find(self, filt=None, projection=None, **kwargs):
        self._db.ops.append(("find", self._name, filt, projection, kwargs))
        out = []
        for raw in self._db.store.get(self._name, []):
            doc = bson.decode(raw, codec_options=CODEC)
            if projection == {"_id": 0}:
                doc.pop("_id", None)
            out.append(doc)
        return iter(out)

    def __getattr__(self, attr):
        raise AssertionError("hier_sync used Collection.%s" % attr)


class _FakeGbmDb:
    def __init__(self, label):
        self.label, self.store, self.indexes, self.seen, self.ops = label, {}, {}, set(), []

    def drop_collection(self, name_or_collection, session=None):
        self.ops.append(("drop_collection", name_or_collection))
        self.store.pop(name_or_collection, None)
        self.indexes.pop(name_or_collection, None)
        self.seen = set()

    def __getitem__(self, name):
        return _FakeGbmCollection(self, name)

    def __getattr__(self, attr):
        raise AssertionError("hier_sync used Database.%s" % attr)


@pytest.fixture
def gbm(monkeypatch):
    """Patches ConnectionFactory.get_mongo_db; yields (dbs by tenant, resolution calls)."""
    monkeypatch.setenv("CNAME", CNAME)
    dbs, calls = {}, []

    def get_mongo_db(tenant, db_type="etl", cname="preprod"):
        calls.append((tenant, db_type, cname))
        return dbs.setdefault(tenant, _FakeGbmDb("gbm:%s" % tenant))

    with mock.patch.object(ConnectionFactory, "get_mongo_db", side_effect=get_mongo_db):
        yield dbs, calls


def _stored(db, coll=HIER_COLL):
    return [bson.decode(raw, codec_options=CODEC) for raw in db.store.get(coll, [])]


def _without_id(docs):
    return [{k: v for k, v in d.items() if k != "_id"} for d in docs]


def _checksum(docs):
    """Legacy get_hier_metadata / SDK create_collection_checksum formula, written out."""
    return hashlib.sha1(json.dumps(_without_id(docs)).encode("utf-8")).hexdigest()


def _records(n, label_size=0):
    recs = []
    for i in range(n):
        rec = {"node": "Global#%06d" % i, "parent": None if i == 0 else "Global#%06d" % ((i - 1) // 4),
               "from": 1514764800 + i, "to": None if i % 7 else 1735689600, "hidden": i % 11 == 0,
               "how": "hier_svc", "label": "Node %d %s" % (i, "x" * label_size)}
        if i % 5 == 0:      # key order varies between documents, as in the app's collection
            rec = dict(reversed(list(rec.items())))
        recs.append(rec)
    return recs[::-1]       # not sorted by node


RICH = [
    {"node": "Global#B", "parent": "Global#!", "from": 0, "to": None, "hidden": False, "label": "Zürich – 東京 ✓ 🚀"},
    {"parent": None, "node": "Global#!", "to": None, "from": 0, "hidden": False, "label": "Global"},
    {"node": "Global#A", "parent": "Global#!", "from": 1514764800, "to": 2 ** 40, "hidden": True, "weight": 0.1,
     "meta": {"z": [1, 2.5, None, -0.0, 2 ** 63 - 1], "a": {"b": "c\u0000d"}, "": "empty key"}},
]


def _expected_ops(batch_sizes):
    return ([("drop_collection", HIER_COLL), ("create_indexes", HIER_COLL, "<legacy indexes>")]
            + [("bulk_write", HIER_COLL, n, False, True) for n in batch_sizes]
            + [("find", HIER_COLL, {}, {"_id": 0}, {})])


def _normalised_ops(db):
    out = []
    for op in db.ops:
        if op[0] == "create_indexes":
            got = [{"key": list(d["key"].items()), **({"unique": d["unique"]} if "unique" in d else {})}
                   for d in op[2]]
            assert got == LEGACY_INDEXES, got
            op = ("create_indexes", op[1], "<legacy indexes>")
        out.append(op)
    return out


# ============================================================================= URL
@pytest.mark.parametrize("path", ["/gbm/hier_sync", "/gbm/hier_sync/"])
def test_url_resolves_with_and_without_trailing_slash(path):
    match = resolve(path)
    assert match.func.view_class is HierarchySyncAPIView
    assert getattr(match.func, "csrf_exempt", False) is True, "the CSRF middleware would refuse the POST (403)"


@pytest.mark.parametrize("path", ["/gbm/hier_syncx", "/gbm/hier_sync/x", "/gbm/xhier_sync"])
def test_lookalikes_are_not_routed(path):
    with pytest.raises(Resolver404):
        resolve(path)


def test_view_configuration_matches_legacy():
    assert HierarchySyncAPIView.http_method_names == ["post"]
    assert HierarchySyncAPIView.restrict_to_roles == {"gnacker"}
    assert HierarchySyncAPIView.pre_validations == [protect_post]
    # protect_post can only refuse when one of these is set, as in legacy (HierarchySync sets neither)
    assert HierarchySyncAPIView.is_protected is False and HierarchySyncAPIView.stage_aware is False


# ============================================================================= service: legacy behaviour
def test_iter_chunks_is_legacy():
    assert list(iter_chunks([], 1000)) == [[]]                     # legacy quirk: one empty batch
    assert [len(b) for b in iter_chunks(list(range(1000)), 1000)] == [1000]
    assert [len(b) for b in iter_chunks(list(range(2000)), 1000)] == [1000, 1000]
    assert [len(b) for b in iter_chunks(list(range(2500)), 1000)] == [1000, 1000, 500]
    assert list(iter_chunks([1, 2, 3, 4, 5, 6, 7])) == [[1, 2, 3, 4, 5], [6, 7]]


@pytest.mark.parametrize("n,batches", [(1, [1]), (999, [999]), (1000, [1000]), (1001, [1000, 1]),
                                       (2000, [1000, 1000]), (2500, [1000, 1000, 500])])
def test_sync_drops_recreates_indexes_inserts_in_batches_then_checksums(gbm, n, batches):
    dbs, calls = gbm
    records = _records(n)
    expected = _checksum(records)

    assert sync_hierarchy(copy.deepcopy(records)) == expected
    db = dbs[TENANT]
    assert calls == [(TENANT, "gbm", CNAME)], "one resolution of the tenant's GBM DB, passed to the checksum"
    assert _normalised_ops(db) == _expected_ops(batches)
    assert _without_id(_stored(db)) == records, "same documents, same order, same key order"


def test_existing_hierarchy_is_replaced_and_other_collections_are_untouched(gbm):
    dbs, _ = gbm
    db = dbs.setdefault(TENANT, _FakeGbmDb("gbm:" + TENANT))
    db.store[HIER_COLL] = [bson.encode({"node": "old"}) for _ in range(3)]
    db.store["hierarchy_backup"] = [bson.encode({"node": "keep"})]

    sync_hierarchy(copy.deepcopy(RICH))
    assert _without_id(_stored(db)) == RICH
    assert _without_id(_stored(db, "hierarchy_backup")) == [{"node": "keep"}]
    assert [op for op in db.ops if op[0] == "drop_collection"] == [("drop_collection", HIER_COLL)]


def test_returned_checksum_is_what_hier_metadata_and_the_sdk_compute(gbm):
    """After a sync the SDK's next comparison (app checksum vs /gbm/hier_metadata) must match, or every run
    would sync again."""
    records = copy.deepcopy(RICH)
    res = sync_hierarchy(copy.deepcopy(records))
    sdk = hashlib.sha1(json.dumps(records).encode("utf-8")).hexdigest()    # create_collection_checksum(FM)
    assert res == sdk == sync_service.get_hier_metadata()


def test_duplicate_records_are_logged_and_the_rest_is_stored_like_legacy(gbm):
    dbs, _ = gbm
    records = _records(1500)
    records.insert(1200, dict(records[3]))                  # duplicate (node, parent, from, to) in batch 2
    records.insert(10, dict(records[0]))                    # and one in batch 1
    with mock.patch.object(sync_service.logger, "error") as error:
        res = sync_hierarchy(copy.deepcopy(records))
    kept = [r for i, r in enumerate(records) if i not in (10, 1201)]
    assert _without_id(_stored(dbs[TENANT])) == kept
    assert res == _checksum(kept)
    assert error.call_count == 2, "one BulkWriteError per affected batch, logged, sync continues"
    assert all(c.args[0]["writeErrors"][0]["code"] == 11000 for c in error.call_args_list)


def test_explicit_db_argument_is_used_like_legacy(gbm):
    _, calls = gbm
    db = _FakeGbmDb("explicit")
    assert sync_hierarchy(copy.deepcopy(RICH), db) == _checksum(RICH)
    assert calls == [] and _without_id(_stored(db)) == RICH


def test_logs_sync_time_like_legacy(gbm):
    with mock.patch.object(sync_service.logger, "warning") as warning:
        sync_hierarchy(copy.deepcopy(RICH))
    msgs = [c.args[0] for c in warning.call_args_list]
    assert len(msgs) == 2 and msgs[0].startswith("Hier Sync Time: ") and msgs[1].startswith("Hier Meta Time: ")


def test_a_db_error_mid_sync_propagates_like_legacy(gbm):
    dbs, _ = gbm
    db = dbs.setdefault(TENANT, _FakeGbmDb("gbm:" + TENANT))
    with mock.patch.object(_FakeGbmCollection, "create_indexes", side_effect=ServerSelectionTimeoutError("down")):
        with pytest.raises(ServerSelectionTimeoutError):
            sync_hierarchy(copy.deepcopy(RICH))


# ============================================================================= service: refusals before the drop
BAD_PAYLOADS = {
    "empty list": [],
    "object": {"node": "Global#!"},
    "string": "Global#!",
    "number": 5,
    "null": None,
    "list of numbers": [1, 2],
    "list with a string": [{"node": "a"}, "b"],
    "list with null": [{"node": "a"}, None],
    "list with a list": [[{"node": "a"}]],
    "int beyond 64 bits": [{"node": "a", "from": 2 ** 63}],
    "negative int beyond 64 bits": [{"node": "a"}, {"node": "b", "to": -(2 ** 63) - 1}],
    "NUL in a key": [{"node": "a", "x\u0000y": 1}],
    "lone surrogate": [{"node": "\ud800"}],
}


@pytest.mark.parametrize("name", list(BAD_PAYLOADS))
def test_payload_legacy_would_fail_on_after_the_drop_is_refused_without_touching_the_db(gbm, name):
    dbs, calls = gbm
    with pytest.raises(InvalidHierarchyPayload):
        sync_hierarchy(copy.deepcopy(BAD_PAYLOADS[name]))
    assert calls == [] and dbs == {}, "refused before the GBM DB is resolved"


@pytest.mark.parametrize("name", [n for n in BAD_PAYLOADS if n not in ("empty list",)])
def test_each_refused_payload_really_fails_in_pymongos_insert_path(name):
    """The refusal list is not wider than pymongo: each of these also fails in the (fake) insert path that
    legacy would reach after dropping the collection."""
    payload = copy.deepcopy(BAD_PAYLOADS[name])
    coll = _FakeGbmDb("probe")[HIER_COLL]
    with pytest.raises(Exception):
        coll.bulk_write([InsertOne(rec) for rec in payload], ordered=False, bypass_document_validation=True)


@pytest.mark.parametrize("payload", [
    [{"node": "a"}],
    copy.deepcopy(RICH),
    [{"node": "a", "from": 2 ** 63 - 1, "to": -(2 ** 63)}],
    [{"node": "nan", "v": float("nan"), "w": float("inf"), "x": float("-inf")}],
    [{"node": "dots", "a.b": 1, "$c": 2, "": 3}],      # pymongo 3.12 inserts with check_keys=False: server decides
    [{"node": "deep", "d": json.loads("[" * 90 + "]" * 90)}],
    [{"node": "big", "label": "x" * 1_000_000}],
    [{"_id": "given", "node": "with-id"}],
    [{}],
])
def test_validation_accepts_everything_pymongo_would_store(payload):
    validate_hier_records(payload)
    coll = _FakeGbmDb("probe")[HIER_COLL]
    coll.bulk_write([InsertOne(rec) for rec in copy.deepcopy(payload)], ordered=False,
                    bypass_document_validation=True)


# ============================================================================= view (RequestFactory)
def _post(body, path="/gbm/hier_sync", tenant=TENANT, content_type="application/json", **extra):
    if tenant is not None:
        extra["HTTP_X_TENANT_NAME"] = tenant
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    request = RequestFactory().post(path, data=data, content_type=content_type, **extra)
    request.user = AnonymousUser()
    return HierarchySyncAPIView.as_view()(request)


@pytest.mark.parametrize("path", ["/gbm/hier_sync", "/gbm/hier_sync/"])
def test_view_returns_the_new_checksum_as_json_like_legacy(gbm, path):
    dbs, calls = gbm
    response = _post(RICH, path)
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    assert response.content.decode() == json.dumps(_checksum(RICH)), "legacy body: a JSON string, quoted hex"
    assert _without_id(_stored(dbs[TENANT])) == RICH
    assert calls == [(TENANT, "gbm", CNAME)]


def test_view_reads_the_raw_body_like_legacy(gbm):
    dbs, _ = gbm
    body = json.dumps(RICH, ensure_ascii=False, indent=2).encode("utf-8")
    assert _post(body).status_code == 200
    assert _without_id(_stored(dbs[TENANT])) == RICH


def test_view_accepts_the_tenant_name_query_parameter(gbm):
    dbs, calls = gbm
    response = _post(RICH, path="/gbm/hier_sync?tenant_name=%s" % TENANT, tenant=None)
    assert response.status_code == 200 and calls == [(TENANT, "gbm", CNAME)]


@pytest.mark.parametrize("tenant", [None, ""])
def test_view_refuses_without_an_explicit_tenant(gbm, tenant):
    dbs, calls = gbm
    response = _post(RICH, tenant=tenant)
    assert response.status_code == 400 and "X-Tenant-Name" in json.loads(response.content)["error"]
    assert calls == [] and dbs == {}


def test_view_refuses_a_tenant_other_than_the_request_context(gbm):
    dbs, calls = gbm
    response = _post(RICH, tenant="other.com")
    assert response.status_code == 400 and calls == [] and dbs == {}


@pytest.mark.parametrize("body", [b"", b"not json", b"[{\"node\": ", b"\xff\xfe\xfa", b"{'node': 'a'}"])
def test_view_refuses_a_body_that_is_not_json(gbm, body):
    dbs, calls = gbm
    response = _post(body)
    assert response.status_code == 400 and "not valid JSON" in json.loads(response.content)["error"]
    assert calls == [] and dbs == {}


@pytest.mark.parametrize("name", list(BAD_PAYLOADS))
def test_view_refuses_bad_payloads_with_400_and_writes_nothing(gbm, name):
    dbs, calls = gbm
    body = json.dumps(BAD_PAYLOADS[name])
    response = _post(body)
    assert response.status_code == 400, response.content
    assert json.loads(response.content)["error"]
    assert calls == [] and dbs == {}


@pytest.mark.parametrize("method", ["get", "head", "put", "patch", "delete"])
def test_view_is_post_only_and_other_methods_touch_nothing(gbm, method):
    dbs, calls = gbm
    request = getattr(RequestFactory(), method)("/gbm/hier_sync", HTTP_X_TENANT_NAME=TENANT)
    request.user = AnonymousUser()
    response = HierarchySyncAPIView.as_view()(request)
    assert response.status_code == 405
    if method in ("get", "head"):
        assert response["Allow"] == "POST"
    assert calls == [] and dbs == {}


def test_view_answers_500_when_the_db_fails_like_legacy(gbm):
    with mock.patch.object(_FakeGbmDb, "drop_collection", side_effect=ServerSelectionTimeoutError("down")):
        response = _post(RICH)
    assert response.status_code == 500, "the SDK's raise_for_status must see the failure"


# ============================================================================= full stack (middleware)
@pytest.fixture
def client(stack_env):
    yield Client(raise_request_exception=True)


@pytest.fixture
def stack_env(monkeypatch):
    """What the deployment provides: the API key, and the user the middleware sets the context with
    (MICROSERVICES_USER, read by aviso.settings at import)."""
    monkeypatch.setenv("INTERNAL_API_KEY", API_KEY)
    monkeypatch.setattr(middlewares, "microservices_user", "unit-test-microservice-user")
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        yield


def _stack_post(client, body, path="/gbm/hier_sync", tenant=TENANT, key=API_KEY,
                content_type="application/json"):
    headers = {}
    if tenant is not None:
        headers["HTTP_X_TENANT_NAME"] = tenant
    if key is not None:
        headers["HTTP_INTERNAL_API_KEY"] = key
    return client.post(path, data=body, content_type=content_type, **headers)


@pytest.mark.parametrize("content_type", ["application/json", "application/x-www-form-urlencoded", "text/plain"])
def test_full_stack_20mb_payload_is_stored(gbm, client, content_type):
    """The SDK posts the whole hierarchy in one request. Django's DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) must
    not apply, whatever the Content-Type: the body is read raw, as in legacy."""
    dbs, _ = gbm
    records = _records(20000, label_size=1000)
    body = json.dumps(records).encode("utf-8")
    assert len(body) > 20 * 1024 * 1024
    response = _stack_post(client, body, content_type=content_type)
    assert response.status_code == 200, response.content[:300]
    assert json.loads(response.content) == _checksum(records)
    assert len(dbs[TENANT].store[HIER_COLL]) == 20000
    assert [op[2] for op in dbs[TENANT].ops if op[0] == "bulk_write"] == [1000] * 20


def test_full_stack_csrf_does_not_block(gbm, stack_env):
    response = _stack_post(Client(enforce_csrf_checks=True, raise_request_exception=True), json.dumps(RICH))
    assert response.status_code == 200


def test_full_stack_without_tenant_header_does_not_sync_the_default_tenant(gbm, client):
    """aviso-core's middleware falls back to aviso.com when X-Tenant-Name is missing (requests drops a header
    whose value is None). hier_sync must refuse instead of rewriting aviso.com's hierarchy."""
    dbs, calls = gbm
    response = _stack_post(client, json.dumps(RICH), tenant=None)
    assert response.status_code == 400 and calls == [] and dbs == {}


@pytest.mark.parametrize("key", [None, "wrong"])
def test_full_stack_requires_the_internal_api_key(gbm, client, key):
    dbs, calls = gbm
    response = _stack_post(client, json.dumps(RICH), key=key)
    assert response.status_code == 401 and calls == [] and dbs == {}


def test_full_stack_get_is_405(gbm, client):
    dbs, calls = gbm
    response = client.get("/gbm/hier_sync", HTTP_X_TENANT_NAME=TENANT, HTTP_INTERNAL_API_KEY=API_KEY)
    assert response.status_code == 405 and calls == [] and dbs == {}


def test_full_stack_tenants_are_isolated(gbm, client):
    dbs, _ = gbm
    other = dbs.setdefault("other.com", _FakeGbmDb("gbm:other.com"))
    other.store[HIER_COLL] = [bson.encode({"node": "other"})]
    assert _stack_post(client, json.dumps(RICH)).status_code == 200
    assert _without_id(_stored(other)) == [{"node": "other"}] and other.ops == []
