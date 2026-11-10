"""``GET /gbm/hier_metadata``, migrated from legacy gbm-service (``gbm/api/hierarchy.py``,
``HierarchyMetadata``).

Returns the SHA-1 checksum of the tenant's GBM ``hierarchy`` collection as a JSON string, the same body
as legacy. The only visible difference is the ``Content-Type`` header: ``application/json`` (legacy sent
Django's default ``text/html``).

``/gbm/hier_sync`` (the POST that rewrites the collection) is not part of this module.
"""
import json
import logging

from django.http.response import HttpResponse, HttpResponseNotAllowed

from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin
from gbm_apis.hierarchy_sync.sync_service import get_hier_metadata

logger = logging.getLogger('gnana.%s' % __name__)


class HierarchyMetadataAPIView(AvisoCompatibilityMixin, AvisoView):
    '''
    Returns the checksum of the hierarchy currently stored in the tenant's GBM DB
    '''
    http_method_names = ['get']
    restrict_to_roles = {AvisoView.Role.Gnacker}
    as_json = False

    def get(self, request, *args, **kwargs):
        res = get_hier_metadata()
        return HttpResponse(json.dumps(res), content_type='application/json')

    def post(self, request, *args, **kwargs):
        # Legacy answered POST with 405 (Django's View.dispatch honours http_method_names). aviso-core's
        # AvisoView.dispatch does not, and post_is_get would run get() for a POST, so refuse it explicitly.
        return HttpResponseNotAllowed(['GET'])
