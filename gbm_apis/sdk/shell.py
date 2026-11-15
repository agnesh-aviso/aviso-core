"""Methods injected into the shell that connects to this service.

avisosdk builds the shell from this package, so anything ``Shell`` itself calls
has to be here. ``Shell.__init__`` calls ``timezone('tenant')`` before a session
exists, and ``Shell.me()`` evaluates ``tenant('get_config', ...)`` eagerly as a
default argument — without both, connect_sdk() raises AttributeError and no one
can reach the service at all.

The endpoints are called through ``shell.api('/gbm/...')``. Named methods for
them land here as the remaining GBM APIs are migrated.
"""
import json
import logging
import os
from urllib.parse import urlencode

# Relative, not absolute: inside the SDK archive this module is
# <prefix>.gbm_apis.sdk.shell and there is no top-level gbm_apis to import.
from ..interface import shellDateUtils
from .data import DatasetPythonSdkFunctions
from .sandbox import SandboxPythonSdkFunctions

logger = logging.getLogger(__name__)

DEFAULT_TIME_ZONE = 'America/Los_Angeles'

TENANT_BASE_PATH = '/gbm/tenant'


def _set_local_timezone(shell):
    """Read the client machine's zone.

    shellDateUtils keeps this as a module function and reaches it through
    ``shell.set_local_timezone()``, which only exists on the legacy shell —
    avisosdk.Shell has no such attribute, so it is inlined here.
    """
    if 'TZ' in os.environ:
        shell._tz_name = os.environ['TZ']
        return shell._tz_name
    try:
        shell._tz_name = '/'.join(os.readlink('/etc/localtime').split('/')[-2:])
    except OSError:
        logger.info('Unable to autodetect the local timezone, using UTC')
        shell._tz_name = 'utc'
    return shell._tz_name


def timezone(shell, tz_str, period_info=False):
    """Set the shell's preferred timezone.

    Mirrors aviso/interface/shellDateUtils.py: 'tenant' resolves from the
    logged-in user, and before login it stays unset rather than failing.
    """
    if tz_str == 'local':
        shell._tz_preference = 'local'
        return _set_local_timezone(shell)

    if tz_str != 'tenant':
        shell._tz_preference = tz_str
        shell._tz_name = tz_str
        return shell._tz_name

    shell._tz_preference = 'tenant'
    if hasattr(shell, '_tz_name'):
        del shell._tz_name

    uinfo = shell.me() if getattr(shell, '_session', None) else None
    if not uinfo:
        logger.debug("Until you login, we can't set tenant timezone.")
        shell._tz_name = None
        return shell._tz_name

    shell._tz_name = uinfo.get('timezone') or DEFAULT_TIME_ZONE
    return shell._tz_name


def tenant(shell, action, **kwargs):
    """The subset of the legacy tenant verb this service answers.

    Kept as a plain function rather than a BaseSDKFunction because
    ``Shell.me()`` calls ``tenant('get_config', ...)`` eagerly, and routing
    that through command metadata would make connect_sdk depend on a command
    this service does not serve.
    """
    if action == 'get_all_config':
        # /gbm/tenant/<name>/config, the legacy /tenant/<name>/config under
        # this app's mount. sandbox= asks for the sandboxed view of the same
        # config, which is the other half of the diff sandbox get_diff prints.
        url = '%s/%s/config' % (TENANT_BASE_PATH, shell._tenant)
        sandbox = kwargs.get('sandbox')
        if sandbox:
            url += '?sandbox=%s' % sandbox
        return shell.api(url, None)

    if action == 'get_config':
        return _get_config(shell, **kwargs)

    if action == 'set_config':
        return _set_config(shell, **kwargs)

    logger.info("tenant('%s') is not served by aviso-core", action)
    return None


def _get_config(shell, category=None, config_name=None, sandbox=None,
                default=None):
    """One config entry, from /gbm/tenant/<name>/config/<category>/<config_name>."""
    if category == 'forecast' and config_name == 'timezone':
        # Shell.me() asks for this on every call, including during login
        # before a tenant is switched to, so it is answered from the shell
        # instead of the server.
        return getattr(shell, '_tz_name', None)
    if not category or not config_name:
        raise Exception("Required arguments 'category' and 'config_name' are mandatory")
    url = '/'.join((TENANT_BASE_PATH, shell._tenant, 'config', category,
                    config_name))
    if sandbox:
        url += '?sandbox=%s' % sandbox
    response = shell.api(url, None)
    return default if response is None else response


def _set_config(shell, category=None, config_name=None, value=None,
                action=None, comment=None, sandbox=None, auto_commit=False):
    """Save, or with action='remove'/'remove_path' delete, one config entry.

    The change goes into ``sandbox`` and returns the old value; it reaches the
    live config when the sandbox is committed, or at once with auto_commit.
    """
    if not category or not config_name:
        raise Exception("Required arguments 'category' and 'config_name' are mandatory")
    if not sandbox:
        raise Exception("Required argument 'sandbox' is mandatory")
    if not action and not value and value is not False:
        raise Exception("Required argument either 'value' or 'action' is mandatory")
    if action == 'save' and not value and value is not False:
        raise Exception("Required argument 'value' must be provided with action = save")
    params = {'sandbox': sandbox}
    if action:
        params['action'] = action
    if comment:
        params['comment'] = comment
    if auto_commit:
        params['auto_commit'] = auto_commit
    if action in ('remove', 'remove_path'):
        value = {}
    url = '/'.join((TENANT_BASE_PATH, shell._tenant, 'config', category,
                    config_name))
    # Posted as a JSON string: the session only sends dicts and lists as they
    # are, so a boolean or number value would otherwise not be posted at all.
    return shell.api(url + '?' + urlencode(params), json.dumps(value))


shell_methods = {
    # Kept local: shellDateUtils.timezone calls shell.set_local_timezone(), a
    # method avisosdk.Shell does not have. Registering it would break
    # connect_sdk(), which calls timezone('tenant') before a session exists.
    'timezone': timezone,
    'tenant': tenant,

    # Client-side date helpers. epoch() reads shell._tz_name and makes no HTTP
    # call, so it needs no endpoint -- but it raises until a tenant is switched
    # to, because _tz_name stays None until then.
    'epoch': shellDateUtils.epoch,
    'epoch2datetime': shellDateUtils.epoch2datetime,
    'epoch2xl': shellDateUtils.epoch2xl,
    'datetime2epoch': shellDateUtils.datetime2epoch,
    'datetime2xl': shellDateUtils.datetime2xl,
    'xl2datetime': shellDateUtils.xl2datetime,

    # Period boundaries, answered by gbm_apis.api.date_manager at /gbm/date/*.
    # Registered as a trio: PeriodInfo.initialize() builds its cache from
    # current + prev + next in one pass, so current_period() alone still
    # reaches /date/next. get_as_of_tuples/generate_as_of_tuples stay
    # unregistered -- they depend on current_period() and can be added now
    # that it resolves.
    'current_period': shellDateUtils.current_period,
    'prev_period': shellDateUtils.prev_period,
    'next_period': shellDateUtils.next_period,

    'dataset': DatasetPythonSdkFunctions(ds_type=''),
    'uipmeta': DatasetPythonSdkFunctions(ds_type='uip'),
    'sourcemeta': DatasetPythonSdkFunctions(ds_type='source'),

    # list, list_available, get_diff and commit -- /sandbox/list and
    # /sandbox/commit are the sandbox endpoints gbm_apis/urls.py serves.
    # See sandbox.py for the commands left out.
    'sandbox': SandboxPythonSdkFunctions(),
}
