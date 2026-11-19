"""Tenant flags: GET/POST /gbm/tenant/<name>/flag/<category>/<config_name>,
and the SDK's tenant('get_flag') / tenant('set_flag') that call it.

Ported from legacy service-infrastructure (TenantFlag in api/tenantmanager.py,
get_flag/set_flag in sdk/tenant.py). Flags live in tenant_flags -- not
tenant_configs, where config lives -- and aviso's postgresdb routes that table
to the context tenant's own GBM stack database.

No database and no network: sec_context and the tenant are fakes that behave
like aviso's Tenant.get_flag / set_flag / remove_flag.

Run from the aviso-core root:

    python -m pytest gbm_apis/tests/test_tenant_flag.py -q -p no:cacheprovider
"""
import json
import os
import types
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")
django.setup()

import pytest  # noqa: E402
from aviso.utils import GnanaError  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.urls import resolve  # noqa: E402

import gbm_apis.api.tenant_config as tenant_config  # noqa: E402
import gbm_apis.sdk.shell as sdk_shell  # noqa: E402

TENANT = "acme.com"
URL = "/gbm/tenant/%s/flag/molecule_status/rtfm" % TENANT


class FakeTenant(object):
    """aviso Tenant's flag methods, over a dict instead of tenant_flags."""

    def __init__(self, flags):
        self.flags = flags

    def get_flag(self, flagset, flag, default=None):
        if (flagset, flag) in self.flags:
            return self.flags[(flagset, flag)]
        if default is not None:
            return default
        raise GnanaError("Key Not Found!!!")

    def set_flag(self, flagset, flag, value):
        old = self.flags.get((flagset, flag))
        self.flags[(flagset, flag)] = value
        return old

    def remove_flag(self, flagset, flag):
        if (flagset, flag) not in self.flags:
            raise GnanaError("Key Not Found!!!")
        return self.flags.pop((flagset, flag))


@pytest.fixture
def flags():
    # A dict value with a 'status' key: AvisoView would mistake that for a
    # ready-made response if the view returned it bare.
    stored = {("molecule_status", "rtfm"): {"status": "finished", "run": 7}}
    context = types.SimpleNamespace(name=TENANT, details=FakeTenant(stored))
    with mock.patch.object(tenant_config, "sec_context", context):
        yield stored


def call(method, query="", body=None, tenant=TENANT):
    factory = RequestFactory()
    path = URL + ("?" + query if query else "")
    if method == "get":
        request = factory.get(path)
    else:
        request = factory.post(path, data=body, content_type="application/json")
    view = tenant_config.TenantFlag()
    handler = getattr(view, method)
    return handler(request, tenant_name=tenant, category="molecule_status",
                   config_name="rtfm")


def body_of(response):
    return json.loads(response.content)


# --- the endpoint ---------------------------------------------------------------

def test_url_routes_to_the_flag_view():
    assert resolve(URL).func.view_class is tenant_config.TenantFlag


def test_get_returns_the_stored_value_even_with_a_status_key(flags):
    response = call("get")
    assert response.status_code == 200
    assert body_of(response) == {"status": "finished", "run": 7}


def test_get_missing_flag_returns_the_default(flags):
    flags.clear()
    assert body_of(call("get", "default=")) == ""
    assert body_of(call("get", "default=False")) == "False"


def test_get_missing_flag_without_default_is_404(flags):
    flags.clear()
    assert call("get").status_code == 404


def test_get_refuses_another_tenant_and_an_action(flags):
    assert call("get", tenant="other.com").status_code == 403
    assert call("get", "action=remove").status_code == 403


def test_post_saves_and_returns_the_old_value(flags):
    response = call("post", body=json.dumps({"status": "running"}))
    assert body_of(response) == {"status": "finished", "run": 7}
    assert flags[("molecule_status", "rtfm")] == {"status": "running"}


def test_post_new_flag_returns_null(flags):
    flags.clear()
    assert body_of(call("post", body=json.dumps("x"))) is None
    assert flags[("molecule_status", "rtfm")] == "x"


def test_post_remove_deletes_and_missing_is_404(flags):
    assert body_of(call("post", "action=remove")) == {"status": "finished", "run": 7}
    assert ("molecule_status", "rtfm") not in flags
    assert call("post", "action=remove").status_code == 404


def test_post_rejects_bad_json_another_tenant_and_unknown_action(flags):
    assert call("post", body="not json").status_code == 400
    assert call("post", body=json.dumps(1), tenant="other.com").status_code == 403
    with pytest.raises(GnanaError):
        call("post", "action=rename", body=json.dumps(1))
    assert flags[("molecule_status", "rtfm")] == {"status": "finished", "run": 7}


# --- the database ---------------------------------------------------------------

def test_flags_are_read_and_written_in_the_tenant_stack_database():
    """tenant_flags goes to the context tenant's GBM stack connection, the
    same as tenant_configs -- never the process's fixed database."""
    from aviso.domainmodel.tenant import TenantFlag
    from aviso.settings import gnana_db2

    table = TenantFlag.getCollectionName()
    assert table == "tenant_flags"
    assert gnana_db2.__connection_for__(table) is gnana_db2.stackscopeddb


# --- the SDK ----------------------------------------------------------------------

@pytest.fixture
def shell():
    calls = []
    return types.SimpleNamespace(
        _tenant=TENANT, calls=calls,
        api=lambda url, data: calls.append((url, data)) or "answer")


def test_sdk_get_flag(shell):
    assert sdk_shell.tenant(shell, "get_flag", category="molecule_status",
                            config_name="rtfm") == "answer"
    sdk_shell.tenant(shell, "get_flag", category="chipotle", config_name="no_cr",
                     default=False)
    assert shell.calls == [
        (URL + "?default=", None),
        ("/gbm/tenant/%s/flag/chipotle/no_cr?default=False" % TENANT, None),
    ]


def test_sdk_set_flag(shell):
    sdk_shell.tenant(shell, "set_flag", category="molecule_status",
                     config_name="rtfm", value={"status": "running"})
    sdk_shell.tenant(shell, "set_flag", category="molecule_status",
                     config_name="rtfm", action="remove")
    assert shell.calls == [
        (URL + "?action=save", json.dumps({"status": "running"})),
        (URL + "?action=remove", "null"),
    ]


def test_sdk_set_config_takes_action_too(shell):
    """tenant()'s own first parameter used to be called action, so
    tenant('set_config', ..., action='remove') failed with "got multiple values
    for argument 'action'" before reaching the server."""
    sdk_shell.tenant(shell, "set_config", category="forecast", config_name="x",
                     action="remove", sandbox="sbx1")
    url, _ = shell.calls[0]
    assert url.startswith("/gbm/tenant/%s/config/forecast/x?" % TENANT)
    assert "action=remove" in url


def test_sdk_flag_commands_need_category_and_name(shell):
    with pytest.raises(Exception):
        sdk_shell.tenant(shell, "get_flag", category="molecule_status")
    with pytest.raises(Exception):
        sdk_shell.tenant(shell, "set_flag", config_name="rtfm", value=1)
    assert shell.calls == []
