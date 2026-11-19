"""``POST /gbm/hier_sync``, migrated from legacy gbm-service (``gbm/api/hierarchy.py``, ``HierarchySync``).

Replaces the tenant's GBM ``hierarchy`` collection with the posted records (drop, recreate the indexes,
insert in batches of 1000) and returns the new checksum as a JSON string, the same body as
``/gbm/hier_metadata``. The aviso-data-sdk sends the app (FM) hierarchy here when the GBM checksum differs
from the app's. The FM hierarchy is never written.

Same as legacy for every request legacy completes: the body is read raw (``request.read()``, so Django's
``DATA_UPLOAD_MAX_MEMORY_SIZE`` does not apply), then ``json.loads`` and ``sync_hierarchy``. Differences,
all refusals that happen before the GBM DB is touched:

* The tenant is the one the middleware resolved (``sec_context``), never read from the request here: a
  session's tenant, a token's, or an Internal-Api-Key caller's ``X-Tenant-Name`` header (or ``tenant_name``
  query parameter). The middleware has no default tenant, so a key request that names none is refused before
  it gets here (it used to fall back to ``aviso.com``, which this endpoint would then have rewritten). A
  header or parameter sent with a session cannot point the rewrite at another tenant.
* A body that is not JSON, or a payload that legacy would only fail on after dropping the collection
  (see ``sync_service.validate_hier_records``), gets a 400 and nothing is written. Legacy answered 500,
  and for the payload cases only after the drop.
* GET answers 405 (legacy did too, through Django's ``http_method_names``; aviso-core's AvisoView
  ignores that list).
* ``csrf_exempt``: legacy had the CSRF middleware disabled. aviso-core enables it, which would refuse
  this POST with a 403.

The response ``Content-Type`` is ``application/json`` (legacy sent Django's default ``text/html``), the
same as ``/gbm/hier_metadata``.
"""
import json
import logging

from django.http.response import HttpResponse, HttpResponseNotAllowed
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from aviso.settings import sec_context
from gbm_apis.framework.baseView import AvisoView, protect_post
from gbm_apis.framework.mixins import AvisoCompatibilityMixin
from gbm_apis.hierarchy_sync.sync_service import InvalidHierarchyPayload, sync_hierarchy

logger = logging.getLogger('gnana.%s' % __name__)


def _bad_request(message):
    return HttpResponse(json.dumps({'error': message}), status=400, content_type='application/json')


@method_decorator(csrf_exempt, name='dispatch')
class HierarchySyncAPIView(AvisoCompatibilityMixin, AvisoView):
    '''
    Replaces the hierarchy stored in the tenant's GBM DB with the posted records
    '''
    http_method_names = ['post']
    restrict_to_roles = {AvisoView.Role.Gnacker}
    pre_validations = [protect_post]
    as_json = False

    def post(self, request, *args, **kwargs):
        # The tenant the middleware resolved for this caller (accounts.context): a key caller must name it
        # in X-Tenant-Name, and a session caller's header is ignored.
        tenant = sec_context.name
        if not tenant or tenant == 'N/A':
            return _bad_request('no tenant for this request: hier_sync rewrites a tenant\'s GBM hierarchy')
        try:
            decoded = json.loads(request.read())
        except ValueError as e:     # JSONDecodeError, or a body that is not UTF-8/16/32
            return _bad_request('request body is not valid JSON: %s' % e)
        logger.info('hier_sync: tenant=%s, %s records', tenant,
                    len(decoded) if isinstance(decoded, list) else type(decoded).__name__)
        try:
            res = sync_hierarchy(decoded)
        except InvalidHierarchyPayload as e:
            logger.error('hier_sync refused for %s, nothing written: %s', tenant, e)
            return _bad_request(str(e))
        return HttpResponse(json.dumps(res), content_type='application/json')

    def get(self, request, *args, **kwargs):
        # Legacy answered GET with 405 (Django's View.dispatch honours http_method_names). aviso-core's
        # AvisoView.dispatch does not, and its default get() would answer 500.
        return HttpResponseNotAllowed(['POST'])
