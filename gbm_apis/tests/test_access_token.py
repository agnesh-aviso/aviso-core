"""The Access-Token checks in gbm_apis/framework/baseView.py.

A service calling as the microservices user (or a script user) has to send
the per-tenant token registered through SetAuthenticationToken, as the
``Access-Token`` header -- the header avisosdk adds. base_validations checks
the token exists for this tenant; protect_post makes a protected POST without
a sandbox need a primary token. Both used to read the header as
request.headers.get('HTTP_ACCESS_TOKEN'), the request.META key name, which
request.headers never has -- so every such call got a 403.

No database and no network: the authorization_tokens table, the stack
Postgres context and sec_context are fakes. Requests are real Django requests
carrying the header the way clients send it.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_access_token.py -q -p no:cacheprovider
"""
import contextlib
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
from aviso.domainmodel.auth_tokens import AuthorizationToken  # noqa: E402
from django.test import RequestFactory  # noqa: E402

import gbm_apis.framework.baseView as baseView  # noqa: E402

TENANT = "acme.com"

# The authorization_tokens rows this tenant's stack holds.
TOKENS = {
    "primary-token": {"source_tenant": TENANT, "primary_tenant": True},
    "secondary-token": {"source_tenant": TENANT, "primary_tenant": False},
    "other-tenants-token": {"source_tenant": "other.com", "primary_tenant": True},
}


def _row(token):
    row = TOKENS.get(token)
    return types.SimpleNamespace(token=token, **row) if row else None


def _get_by_specified_criteria(criteria):
    row = _row(criteria.get("object.token"))
    if row and row.source_tenant == criteria.get("object.source_tenant"):
        return row
    return None


def _get_by_field_value(field, value):
    assert field == "token"
    return _row(value)


@pytest.fixture(autouse=True)
def env():
    tenant_context = types.SimpleNamespace(
        name=TENANT, details=types.SimpleNamespace(name=TENANT))
    with mock.patch.object(baseView, "CNAME", "app"), \
            mock.patch.object(baseView, "sec_context", tenant_context), \
            mock.patch.object(baseView, "TenantStackPostgresContext",
                              lambda tenant: contextlib.nullcontext()), \
            mock.patch.object(AuthorizationToken, "getBySpecifiedCriteria",
                              side_effect=_get_by_specified_criteria) as lookup, \
            mock.patch.object(AuthorizationToken, "getByFieldValue",
                              side_effect=_get_by_field_value):
        yield lookup


def service_user():
    return types.SimpleNamespace(is_authenticated=True,
                                 username=baseView.microservices_user)


def session_user():
    return types.SimpleNamespace(is_authenticated=True, username="someone@acme.com")


def make_request(method="get", token=None, sandbox=None, user=None):
    headers = {"Access-Token": token} if token else {}
    # In the URL for both methods: RequestFactory().post() would put a data
    # dict in the body, and the views read sandbox from the query string.
    path = "/gbm/sandbox/list" + ("?sandbox=%s" % sandbox if sandbox else "")
    request = getattr(RequestFactory(), method)(path, headers=headers)
    request.params = request.GET.copy()
    request.user = user or service_user()
    return request


# --- base_validations ---------------------------------------------------------

def test_registered_token_is_accepted(env):
    baseView.base_validations(make_request(token="primary-token"))

    env.assert_called_once_with({"object.token": "primary-token",
                                 "object.source_tenant": TENANT})


def test_unknown_token_is_refused():
    with pytest.raises(baseView.ForbiddenError) as refused:
        baseView.base_validations(make_request(token="no-such-token"))
    assert refused.value.status_code == 403


def test_missing_token_is_refused():
    with pytest.raises(baseView.ForbiddenError):
        baseView.base_validations(make_request())


def test_token_registered_for_another_tenant_is_refused():
    with pytest.raises(baseView.ForbiddenError):
        baseView.base_validations(make_request(token="other-tenants-token"))


def test_protected_call_needs_a_primary_token_outside_a_sandbox():
    baseView.base_validations(make_request(token="primary-token"), is_protected=True)
    with pytest.raises(baseView.ForbiddenError, match="PRIMARY"):
        baseView.base_validations(make_request(token="secondary-token"),
                                  is_protected=True)
    baseView.base_validations(make_request(token="secondary-token", sandbox="sbx1"),
                              is_protected=True)


def test_session_user_is_not_asked_for_a_token(env):
    baseView.base_validations(make_request(user=session_user()))

    env.assert_not_called()


# --- protect_post -------------------------------------------------------------

def test_primary_token_may_post_to_main_data():
    baseView.protect_post(make_request("post", token="primary-token"),
                          is_protected=True)


def test_secondary_token_may_not_post_to_main_data():
    with pytest.raises(baseView.ForbiddenError, match="PRIMARY"):
        baseView.protect_post(make_request("post", token="secondary-token"),
                              is_protected=True)


def test_secondary_token_may_post_to_a_sandbox():
    baseView.protect_post(make_request("post", token="secondary-token", sandbox="sbx1"),
                          is_protected=True)


# Legacy's rule (service-infrastructure views.py protect_post): a request that
# carries a token is checked even when its session user is not a service user.
def test_session_user_with_a_secondary_token_may_not_post_to_main_data():
    with pytest.raises(baseView.ForbiddenError, match="PRIMARY"):
        baseView.protect_post(
            make_request("post", token="secondary-token", user=session_user()),
            is_protected=True)


def test_session_user_without_a_token_is_not_checked():
    baseView.protect_post(make_request("post", user=session_user()),
                          is_protected=True)
