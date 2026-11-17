"""The sandbox endpoints, ported from service-infrastructure/aviso/api/sandbox_manager.py.

``list`` and ``commit`` are served. The legacy module also exposed
/sandbox/purge, /sandbox/resync, /sandbox/clear_data and /sandbox/list_stale;
those views are not ported yet, so the SDK module alongside this one registers
only the commands it can actually reach.

list is here because commit needs it: the SDK checks the sandbox exists before
it shows a diff, and get_diff reads the list to validate its argument.
"""
import json
import logging

from django.http import (HttpResponseForbidden, HttpResponseNotFound,
                         JsonResponse)
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from aviso.domainmodel.sandbox import Sandbox
from aviso.settings import gnana_db, sec_context
from aviso.utils import is_prod, is_true

from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin

logger = logging.getLogger('gnana.%s' % __name__)

# Suffixes the platform uses for its own collections. A sandbox named one of
# these would drop live data rather than a sandboxed copy.
RESERVED_SANDBOX_NAMES = ['_data', '_run', '_result', '_rej', 'main',
                          'expected', 'result_cache', 'labels']

# Where a sandbox's own collections carry its name: the dot-separated part this
# many places after the marker part. Counted from the marker, not from the
# start, because the tenant name has dots of its own.
#   <tenant>.<ds>._uip.<sbx>                         gbm_apis/domainmodel/datameta.py, InboxEntryClass
#   <tenant>.<ds>._results.<model>.<sbx>[.<quarter>] gbm_apis/deal_result/dataset.py, col_type '_result'
#   <tenant>.<ds>._runs.<model>.<sbx>                gbm_apis/deal_result/dataset.py, col_type '_run'
#   <tenant>.<ds>._rejs.<model>.<sbx>                col_type '_rej' (legacy)
#   <tenant>.combined_results.<target>.<key>.<sbx>   gbm_apis/forecast_result/combined_results.py
SANDBOX_NAME_POSITION = {'_uip': 1, '_results': 2, '_runs': 2, '_rejs': 2,
                         'combined_results': 3}

# Result models whose collections end in a quarter after the sandbox
# (gbm_apis/deal_result/dataset.py, compressed results).
QUARTERED_RESULT_MODELS = {'existingpipeforecast'}


def is_sandbox_collection(collection, sandbox_name):
    """Whether ``collection`` is one of ``sandbox_name``'s own collections.

    The sandbox has to sit exactly where the naming above puts it. Legacy
    matched '.<sandbox>' anywhere in a _results name, so a sandbox sharing a
    name with a dataset, model, target, quarter or part of the tenant name
    ('opps', 'bookings', 'com', ...) dropped live results. A name of any other
    shape is left alone.
    """
    parts = collection.split('.')
    for marker, offset in SANDBOX_NAME_POSITION.items():
        if marker not in parts:
            continue
        at = parts.index(marker) + offset
        if at >= len(parts) or parts[at] != sandbox_name:
            return False
        trailing = len(parts) - at - 1
        if trailing == 0:
            return True
        return (marker == '_results' and trailing == 1
                and parts[at - 1] in QUARTERED_RESULT_MODELS)
    return False


def clear_sandbox_data(sandbox_name):
    """Drop the collections a sandbox accumulated, along with its record.

    The collections are listed and dropped in the tenant's GBM database, and
    only those is_sandbox_collection() recognises as the sandbox's own.

    The sandbox row goes first even when the name turns out to be reserved,
    which is what the legacy implementation does -- a reserved name still
    leaves the sandbox removed, only its collections are left alone.
    """
    response = {'success': False, 'collections_dropped': {}}
    sandbox = Sandbox.getByName(sandbox_name)
    if not sandbox:
        response['reason'] = 'sandox not available'
        return response
    Sandbox.remove(sandbox.id)

    if sandbox_name in RESERVED_SANDBOX_NAMES:
        response['reason'] = "sandbox name shouldn't be one of predefined keys"
        return response

    # Lists the GBM database the drop runs in (aviso 0.1.13 with the
    # collection_names fix; it used to list the FM Mongo database in production).
    for collection in gnana_db.collection_names('%s' % sec_context.name):
        if is_sandbox_collection(collection, sandbox_name):
            response['collections_dropped'][collection] = gnana_db.dropCollection(
                collection)
    response['success'] = True
    return response


class ListSandbox(AvisoCompatibilityMixin, AvisoView):
    """The tenant's open sandboxes, each with the changes it holds."""

    http_method_names = ['get']

    restrict_to_roles = {AvisoView.Role.Administrator, AvisoView.Role.Gnacker,
                         AvisoView.Role.User}

    def get(self, request, *args, **kwargs):
        """Get the list of sandboxes for the current tenant
        """
        sandbox_list = []
        for sandbox in Sandbox.getAll({'status': Sandbox.SANDBOX_CREATED_STATUS}):
            all_changes = []
            for change in sandbox.changes_in_sandbox():
                all_changes.append({change.path: change.changes,
                                    'action': change.action})
            sandbox_list.append({sandbox.name: all_changes})

        return JsonResponse(sandbox_list, safe=False)


@method_decorator(csrf_exempt, name='dispatch')
class CommitSandbox(AvisoCompatibilityMixin, AvisoView):
    """Approve a sandbox's changes and fold them into the tenant config."""

    http_method_names = ['post']

    restrict_to_roles = {AvisoView.Role.Administrator, AvisoView.Role.Gnacker}

    def post(self, request, *args, **kwargs):
        """Approve the sandbox changes
        """
        post_data = json.loads(request.read() or b'{}')
        sandbox_name = post_data.get('sandbox_name')
        clear_data = is_true(post_data.get('clear_sandbox_data', 'false'))
        is_self_serve_call = is_true(post_data.get('is_self_serve_call', 'false'))
        # Try to get the sandbox basing on name
        sandbox = Sandbox.getByName(sandbox_name)
        if not sandbox:
            return HttpResponseNotFound("Sandbox not Found!!")
        # On prod the change needs a second pair of eyes, unless self serve
        # drove the commit itself.
        if is_prod() and not is_self_serve_call:
            if request.user.username == sandbox.creator:
                return HttpResponseForbidden("Creator is not allowed to commit changes")
        resp = {'success': True}
        run_prepare = None
        purge_sandbox_data = {}
        try:
            result = Sandbox.commit(sandbox_name)
            if result['run_prepare']:
                run_prepare = True
            if clear_data:
                purge_sandbox_data = clear_sandbox_data(sandbox_name)
        except Exception as e:
            logger.exception(e)
            resp = {'success': False,
                    'Error': str(e)
                    }
        resp['purge_sandbox_data'] = purge_sandbox_data
        if run_prepare:
            resp.update({'message': 'Please run a full prepare ( with out sandbox ) \
                                    again to reflect your changes in UIP'})
        return JsonResponse(resp)
