"""Which tenant basic_results answers for.

The middleware (accounts.context.resolve) decides the request's tenant and puts
it in sec_context:

    1. a logged-in session   -> the session's tenant; X-Tenant-Name is ignored
    2. an Access-Token       -> the token's tenant
    3. an Internal-Api-Key   -> the X-Tenant-Name header, or ?tenant_name=;
                                naming neither is refused (no default tenant)

Each caller's tenant comes from its own source; they never mix.

deals_results reads that context. basic_results (DataLoadAPIView) used to read
the X-Tenant-Name header itself, so a session caller -- the SDK sends no such
header -- got a 400, and one that did send it could name another tenant than
the one it is logged in to. It now reads sec_context like deals_results.

No database and no network: DataLoad and RevenueSchedule are mocks.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_basic_results_tenant.py -q -p no:cacheprovider
"""
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
from django.test import RequestFactory  # noqa: E402

import gbm_apis.api.data_load as data_load  # noqa: E402
from accounts import context  # noqa: E402
from accounts.session_utils import (LOGIN_TENANT_NAME, LOGIN_USER_NAME,  # noqa: E402
                                    SWITCH_TYPE, TENANT_NAME, TENANT_USERNAME)

URL = "/gbm/basic_results?period=2027Q4&from_timestamp=1"


@pytest.fixture
def loader():
    """DataLoad as a mock, so the test sees the tenant basic_results asks for."""
    data_loader = mock.Mock()
    data_loader.return_value.get_basic_results.return_value = {"opp1": {}}
    scheduler = mock.Mock()
    scheduler.return_value.revenue_schedule.return_value = {"opp1": {}}
    with mock.patch.object(data_load, "DataLoad", data_loader), \
            mock.patch.object(data_load, "RevenueSchedule", scheduler), \
            mock.patch.dict(os.environ, {"STACK": "gbm1-ms"}):
        yield data_loader


def basic_results(context_tenant, header=None):
    """GET basic_results with the middleware's tenant set to context_tenant."""
    headers = {"X-Tenant-Name": header} if header else {}
    request = RequestFactory().get(URL, headers=headers)
    with mock.patch.object(data_load, "sec_context",
                           types.SimpleNamespace(name=context_tenant)):
        return data_load.DataLoadAPIView().get(request)


# --- basic_results ----------------------------------------------------------------

def test_session_caller_without_a_header_gets_its_tenant(loader):
    response = basic_results("acme.com")

    assert response.status_code == 200
    assert loader.call_args.kwargs["tenant_name"] == "acme.com"


def test_a_header_cannot_point_the_request_at_another_tenant(loader):
    basic_results("acme.com", header="other.com")

    assert loader.call_args.kwargs["tenant_name"] == "acme.com"


def test_no_tenant_in_context_is_400(loader):
    assert basic_results("N/A").status_code == 400
    loader.assert_not_called()


# --- the middleware decides the tenant ----------------------------------------------

def test_middleware_session_wins_over_the_header():
    request = RequestFactory().get(URL, headers={"X-Tenant-Name": "other.com"})
    request.user = types.SimpleNamespace(is_authenticated=True,
                                         username="someone@acme.com")
    request.session = {TENANT_NAME: "acme.com", LOGIN_TENANT_NAME: "acme.com",
                       TENANT_USERNAME: "someone@acme.com",
                       LOGIN_USER_NAME: "someone@acme.com", SWITCH_TYPE: "tenant"}
    with mock.patch.object(context.sec_context, "set_context") as set_context:
        tenant, how = context.resolve(request)

    assert (tenant, how) == ("acme.com", "session")
    assert set_context.call_args.args[1] == "acme.com"


def test_middleware_session_wins_even_when_an_internal_key_is_sent_too():
    request = RequestFactory().get(
        URL, headers={"Internal-Api-Key": "unit-key", "X-Tenant-Name": "other.com"})
    request.user = types.SimpleNamespace(is_authenticated=True,
                                         username="someone@acme.com")
    request.session = {TENANT_NAME: "acme.com", LOGIN_TENANT_NAME: "acme.com",
                       TENANT_USERNAME: "someone@acme.com",
                       LOGIN_USER_NAME: "someone@acme.com", SWITCH_TYPE: "tenant"}
    with mock.patch.dict(os.environ, {"INTERNAL_API_KEY": "unit-key"}), \
            mock.patch.object(context.sec_context, "set_context"):
        assert context.resolve(request) == ("acme.com", "session")


def key_request(query="", tenant_header=None):
    headers = {"Internal-Api-Key": "unit-key"}
    if tenant_header:
        headers["X-Tenant-Name"] = tenant_header
    request = RequestFactory().get("/gbm/basic_results" + query, headers=headers)
    request.user = types.SimpleNamespace(is_authenticated=False)
    return request


def test_middleware_internal_key_caller_naming_no_tenant_is_refused():
    """Neither header nor ?tenant_name=: refused, not run against a default tenant."""
    with mock.patch.dict(os.environ, {"INTERNAL_API_KEY": "unit-key"}), \
            mock.patch.object(context.sec_context, "set_context") as set_context:
        assert context.resolve(key_request()) == (None, None)
    set_context.assert_not_called()


def test_middleware_internal_key_caller_may_use_the_tenant_name_parameter():
    with mock.patch.dict(os.environ, {"INTERNAL_API_KEY": "unit-key"}), \
            mock.patch.object(context.sec_context, "set_context"):
        assert context.resolve(key_request("?tenant_name=acme.com")) == ("acme.com", "key")


def test_middleware_session_ignores_the_tenant_name_parameter():
    request = RequestFactory().get(URL + "&tenant_name=other.com")
    request.user = types.SimpleNamespace(is_authenticated=True,
                                         username="someone@acme.com")
    request.session = {TENANT_NAME: "acme.com", LOGIN_TENANT_NAME: "acme.com",
                       TENANT_USERNAME: "someone@acme.com",
                       LOGIN_USER_NAME: "someone@acme.com", SWITCH_TYPE: "tenant"}
    with mock.patch.object(context.sec_context, "set_context"):
        assert context.resolve(request) == ("acme.com", "session")


def test_middleware_internal_key_caller_gets_the_header_tenant():
    request = RequestFactory().get(
        URL, headers={"Internal-Api-Key": "unit-key", "X-Tenant-Name": "acme.com"})
    request.user = types.SimpleNamespace(is_authenticated=False)
    with mock.patch.dict(os.environ, {"INTERNAL_API_KEY": "unit-key"}), \
            mock.patch.object(context.sec_context, "set_context") as set_context:
        tenant, how = context.resolve(request)

    assert (tenant, how) == ("acme.com", "key")
    assert set_context.call_args.kwargs["tenant_name"] == "acme.com"
