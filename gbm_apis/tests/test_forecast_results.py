"""Tests for ``GET /gbm/forecast_results``. No database and no network: Postgres and Mongo are faked.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_forecast_results.py -q -p no:cacheprovider
"""
import base64
import datetime
import json
import os
import zlib
from collections import namedtuple
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pymongo  # noqa: E402
import pytest  # noqa: E402
from bson import BSON  # noqa: E402
from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.urls import resolve  # noqa: E402

from aviso.framework import tenant_holder  # noqa: E402
from aviso.framework.connection_factory import ConnectionFactory  # noqa: E402
from aviso.utils import GnanaError, dateUtils  # noqa: E402
from aviso.utils.dateUtils import datetime2epoch  # noqa: E402

import gbm_apis.api.forecast_results as forecast_results_api  # noqa: E402
import gbm_apis.deal_result.result_Utils as result_utils  # noqa: E402
from gbm_apis.api.forecast_results import ForecastResultsAPIView  # noqa: E402
from gbm_apis.forecast_result.combined_results import _find_gbm_docs  # noqa: E402
from utils.db_read_utils import GbmTenantConfigReader  # noqa: E402

TENANT = "acme.com"


class _FmTenant:
    """``sec_context.details`` stand-in, so nothing reaches the FM Postgres."""

    def get_config(self, category, config_name=None, default=None, sandbox=None):
        return default

    def get_flag(self, flagset, flag, default=None):
        return default


@pytest.fixture(autouse=True)
def tenant_context():
    tenant_holder.set_context(user_name="test", tenant_name=TENANT, login_tenant_name=TENANT)
    tenant_holder.thread_local.details.tenant_details = _FmTenant()
    yield


# ----------------------------------------------------------------------------- fake GBM Postgres
class _FakePgCursor:
    def __init__(self, conn):
        self.conn, self.row = conn, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if not (self.conn.readonly is True and self.conn.autocommit is False):
            raise AssertionError("query sent on a connection that is not read-only")
        self.conn.sql.append((sql, params))
        table = "tenant_flags" if "tenant_flags" in sql else "tenant_configs"
        key = (table, params[1], params[2])
        self.row = {"value": json.dumps(self.conn.rows[key])} if key in self.conn.rows else None

    def fetchone(self):
        return self.row


class _FakePgConn:
    def __init__(self, rows, stack):
        self.rows, self.stack = rows, stack
        self.readonly, self.autocommit = None, True
        self.sql, self.rollbacks, self.closed = [], 0, False

    def set_session(self, readonly=None, autocommit=None):
        self.readonly, self.autocommit = readonly, autocommit

    def cursor(self, cursor_factory=None):
        assert not self.closed
        return _FakePgCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def gbm_pg():
    """Patch ConnectionFactory's Postgres side; yields (rows, opened_connections, stack_calls)."""
    rows, conns, stack_calls = {}, [], []

    def get_ms_stack(tenant, db_type):
        stack_calls.append((tenant, db_type))
        return "gbm2-ms"

    def get_pg_conn(db_type, stack=None):
        assert db_type == "gbm"
        conns.append(_FakePgConn(rows, stack))
        return conns[-1]

    with mock.patch.object(ConnectionFactory, "_get_ms_stack", side_effect=get_ms_stack), \
            mock.patch.object(ConnectionFactory, "_get_pg_conn", side_effect=get_pg_conn):
        yield rows, conns, stack_calls


# ----------------------------------------------------------------------------- fake GBM Mongo
class _FakeCursor:
    def __init__(self, docs, kwargs):
        self.docs, self.kwargs, self.closed = iter(docs), kwargs, False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.docs)

    def close(self):
        self.closed = True


def _get_path(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def _matches(doc, criteria):
    for key, value in criteria.items():
        if key == "$and":
            if not all(_matches(doc, c) for c in value):
                return False
        elif key == "$or":
            if not any(_matches(doc, c) for c in value):
                return False
        elif _get_path(doc, key) != value:
            return False
    return True


@pytest.fixture
def gbm_mongo():
    """Patch ConnectionFactory.get_mongo_db; yields (collections, cursors, db_calls)."""
    collections, cursors, db_calls = {}, [], []

    class _Coll:
        def __init__(self, name):
            self.name = name

        def find(self, criteria, sort=None, **kwargs):
            docs = [d for d in collections.get(self.name, []) if _matches(d, criteria)]
            for field, direction in reversed(sort or []):
                docs.sort(key=lambda d: _get_path(d, field), reverse=direction == pymongo.DESCENDING)
            cursors.append(_FakeCursor([dict(d) for d in docs], kwargs))
            return cursors[-1]

    class _Db:
        def __getitem__(self, name):
            return _Coll(name)

    def get_mongo_db(tenant, db_type="etl", cname="preprod"):
        db_calls.append((tenant, db_type, cname))
        return _Db()

    with mock.patch.object(ConnectionFactory, "get_mongo_db", side_effect=get_mongo_db):
        yield collections, cursors, db_calls


def _encrypt(obj):
    return base64.b64encode(zlib.compress(BSON.encode(obj))).decode()


# ============================================================================= GbmTenantConfigReader
def test_reader_reads_gbm_postgres_read_only_and_closes(gbm_pg):
    rows, conns, stack_calls = gbm_pg
    rows[("tenant_configs", "forecast", "tenant")] = {"forecast_period": "Q"}

    with GbmTenantConfigReader(TENANT) as reader:
        assert reader.get_config("forecast", "tenant") == {"forecast_period": "Q"}
        assert reader.get_config("forecast", "missing", "fallback") == "fallback"
        assert reader.stack == "gbm2-ms"

    assert stack_calls == [(TENANT, "gbm")]
    assert len(conns) == 1, "one connection per reader"
    conn = conns[0]
    assert conn.stack == "gbm2-ms"
    assert conn.readonly is True and conn.autocommit is False
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in conn.sql)
    assert all(params[0] == TENANT for _, params in conn.sql)
    assert conn.rollbacks == len(conn.sql) == 2, "every read ends its read-only transaction"
    assert conn.closed


def test_reader_opens_no_connection_when_nothing_is_read(gbm_pg):
    _, conns, stack_calls = gbm_pg
    with GbmTenantConfigReader(TENANT):
        pass
    assert conns == [] and stack_calls == []


def test_reader_closes_connection_on_error(gbm_pg):
    _, conns, _ = gbm_pg
    with pytest.raises(ValueError):
        with GbmTenantConfigReader(TENANT) as reader:
            reader.get_config("forecast", "tenant")
            raise ValueError("boom")
    assert conns[0].closed


def test_reader_matches_legacy_get_config_and_get_flag(gbm_pg):
    rows, _, _ = gbm_pg
    rows[("tenant_configs", "gbm_cfg", "dd")] = {"hier": ["Hier", "Seg"]}
    rows[("tenant_flags", "save_on_error", "bookings")] = {"drilldowns": 1}

    with GbmTenantConfigReader(TENANT) as reader:
        # dotted names walk into the stored value; a missing key gives the default (legacy find_in_config)
        assert reader.get_config("gbm_cfg", "dd.hier") == ["Hier", "Seg"]
        assert reader.get_config("gbm_cfg", "dd.nope", "dflt") == "dflt"
        assert reader.get_flag("save_on_error", "bookings", {}) == {"drilldowns": 1}
        assert reader.get_flag("save_on_error", "other", {}) == {}
        with pytest.raises(GnanaError):
            reader.get_flag("save_on_error", "other")
        with pytest.raises(NotImplementedError):
            reader.get_config("forecast")


# ============================================================================= Mongo access
def test_find_gbm_docs_uses_gbm_db_decrypts_and_closes(gbm_mongo):
    collections, cursors, db_calls = gbm_mongo
    full = {"k": "secret", "n": 1}
    collections["c"] = [{"_id": 1, "object": {"k": "plain", "n": 1}},
                        {"_id": 2, "object": {"n": 1}, "_encdata": _encrypt(full)}]

    docs = list(_find_gbm_docs("c", {"object.n": 1}, sort=[("_id", pymongo.ASCENDING)]))

    assert [d["object"] for d in docs] == [{"k": "plain", "n": 1}, full]
    assert all("_encdata" not in d for d in docs)
    assert db_calls == [(TENANT, "gbm", os.environ.get("CNAME", "preprod"))]
    assert cursors[0].kwargs == {"batch_size": 500, "no_cursor_timeout": True}
    assert cursors[0].closed


def test_find_gbm_docs_closes_cursor_when_caller_stops_early(gbm_mongo):
    collections, cursors, _ = gbm_mongo
    collections["c"] = [{"_id": i, "object": {}} for i in range(3)]
    gen = _find_gbm_docs("c", {})
    next(gen)
    gen.close()
    assert cursors[0].closed, "no_cursor_timeout cursors must always be closed"


# ============================================================================= view + URL
def _get(params):
    request = RequestFactory().get("/gbm/forecast_results", params)
    request.user = AnonymousUser()
    return ForecastResultsAPIView.as_view()(request)


@pytest.mark.parametrize("path", ["/gbm/forecast_results", "/gbm/forecast_results/"])
def test_url_resolves_with_and_without_trailing_slash(path):
    assert resolve(path).func.view_class is ForecastResultsAPIView


def test_view_passes_parameters_like_legacy_and_returns_json(gbm_pg):
    calls = []
    result = {"2026Q4": {"Global#!": {"tot_won_and_fcst": [1.5]}}}

    def fake_gimme(report_type, periods, gbm_config):
        calls.append((report_type, periods, gbm_config))
        return result

    with mock.patch.object(forecast_results_api, "gimme_forecast_results", side_effect=fake_gimme):
        default = _get({"period": ["202610", "2026Q4"]})
        expected = _get({"period": "2026Q4", "report_type": "expected"})

    assert default.status_code == 200 and default["Content-Type"] == "application/json"
    assert default.content.decode() == json.dumps(result)
    assert calls[0][:2] == ("bookings", ["202610", "2026Q4"]), "report_type defaults to bookings"
    assert calls[1][:2] == ("expected", ["2026Q4"])
    assert isinstance(calls[0][2], GbmTenantConfigReader) and calls[0][2].tenant_name == TENANT
    assert expected.status_code == 200


def test_view_is_get_only_like_legacy(gbm_pg):
    _, conns, _ = gbm_pg
    request = RequestFactory().post("/gbm/forecast_results?period=2026Q4")
    request.user = AnonymousUser()
    response = ForecastResultsAPIView.as_view()(request)
    assert response.status_code == 405 and response["Allow"] == "GET"
    assert conns == [], "a refused POST must not touch the database"


# ============================================================================= end to end
def test_end_to_end_groups_nodes_and_fills_gaps(gbm_pg, gbm_mongo):
    rows, conns, _ = gbm_pg
    collections, cursors, _ = gbm_mongo
    rows[("tenant_configs", "forecast", "tenant")] = {"forecast_period": "Q"}
    rows[("tenant_configs", "datasets", "AvisoTaskDS")] = {
        "name": "AvisoTaskDS", "params": {"general": {"globals": {"run_blackswan": False}}}, "models": []}
    rows[("tenant_configs", "datasets", "OppDS")] = {
        "name": "OppDS", "params": {"general": {}},
        "models": [{"name": "common", "type": "common", "config": {}, "field_map": {}}]}
    rows[("tenant_configs", "target_spec", "bookings")] = {
        "name": "bookings", "models": {}, "module": "", "keys": {}, "drilldowns": ["Hier"],
        "report_spec": {"detailed_bookings": {"as_of": "v[2]['as_of']",
                                              "tot_won_and_fcst": "v[3].get('tot_won_and_fcst', 0)"}}}

    begins, horizon = datetime.datetime(2026, 10, 1), datetime.datetime(2026, 12, 31, 23, 59, 59)
    day1, day2 = begins, begins + datetime.timedelta(days=1)

    def doc(dim, node, as_of, tot, encrypt=False):
        fdv = "." if dim == "." else f"{dim}~::~{node}"
        obj = {"dim_attrs": {"full_dim_type": dim, "full_dim_val": fdv, "final_dim_val": fdv, "dimensions": [fdv]},
               "as_of": as_of, "begins": begins, "horizon": horizon, "as_of_mnemonic": "2026Q4",
               "run_time_horizon": "custom_ck1", "extended_attrs": {"tot_won_and_fcst": tot}}
        if encrypt:
            return {"object": {k: obj[k] for k in ("dim_attrs", "as_of", "begins", "horizon", "as_of_mnemonic")},
                    "_encdata": _encrypt(obj)}
        return {"object": obj}

    collections[f"{TENANT}.combined_results.bookings.main"] = [
        doc(".", ".", day1, 0), doc(".", ".", day2, 0),
        doc("Hier", "Global#!", day1, 10.0), doc("Hier", "Global#!", day2, 20.0, encrypt=True),
        doc("Hier", "Global#A", day1, 5.0),  # no day2 doc: filled with the default 0
        doc("Hier", "Global#A", day1, 99.0) | {"object": {**doc("Hier", "Global#A", day1, 99.0)["object"],
                                                          "as_of_mnemonic": "2026Q3"}},  # other quarter
    ]

    period = namedtuple("P", "mnemonic begin end")("2026Q4", begins, horizon)
    with mock.patch.object(result_utils, "current_period", return_value=period):
        response = _get({"period": "2026Q4"})

    assert response.status_code == 200, response.content
    body = json.loads(response.content)
    epoch = [datetime2epoch(dateUtils.from_db(d)) for d in (day1, day2)]
    assert body == {"2026Q4": {"Global#A": {"as_of": epoch, "tot_won_and_fcst": [5.0, 0]},
                               "Global#!": {"as_of": epoch, "tot_won_and_fcst": [10.0, 20.0]}}}
    assert list(body["2026Q4"]) == ["Global#A", "Global#!"], "node order follows the Mongo sort (dim value DESC)"
    assert len(conns) == 1 and conns[0].closed and conns[0].readonly is True
    assert cursors and all(c.closed for c in cursors)
