"""``GET /gbm/forecast_results``, migrated from legacy gbm-service (``gbm/api/cache_managers.py``,
``ForecastResults``).

Same parameters and the same JSON body as legacy. The only visible difference is the
``Content-Type`` header: ``application/json`` (legacy sent Django's default ``text/html``).
"""
import json
import logging

from django.http.response import HttpResponse, HttpResponseNotAllowed

from aviso.settings import sec_context
from gbm_apis.forecast_result.forecast_results import gimme_forecast_results
from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin
from utils.db_read_utils import GbmTenantConfigReader

logger = logging.getLogger('gnana.%s' % __name__)


class ForecastResultsAPIView(AvisoCompatibilityMixin, AvisoView):
    '''
    Fetches the combined results

    Parameters:
        period {str} - A period to grab all the results for, can either be a month or a quarter
        report_type {str} - Which report type you'd like to grab results for, default is bookings

    Returns:
        A dict of results, keyed by the hierarchy

    '''
    http_method_names = ['get']
    restrict_to_roles = {AvisoView.Role.Gnacker}
    as_json = False

    def get(self, request, *args, **kwargs):
        report_type = request.GET.get('report_type', 'bookings')
        periods = request.GET.getlist('period', [])
        # GBM-side configs (target_spec, datasets, forecast/tenant) come from the tenant's GBM Postgres,
        # read-only, over one connection for the whole request.
        with GbmTenantConfigReader(sec_context.name) as gbm_config:
            res = gimme_forecast_results(report_type, periods, gbm_config)
        return HttpResponse(json.dumps(res), content_type='application/json')

    def post(self, request, *args, **kwargs):
        # Legacy answered POST with 405 (Django's View.dispatch honours http_method_names). aviso-core's
        # AvisoView.dispatch does not, and post_is_get would run get() for a POST, so refuse it explicitly.
        return HttpResponseNotAllowed(['GET'])
