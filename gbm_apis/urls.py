from django.urls import path, re_path
from gbm_apis.api.drilldown_fields_v2 import DrilldownFieldsV2
from gbm_apis.api.data_load import DataLoadAPIView
from gbm_apis.api.deals_results import DealsResultsAPIView
from gbm_apis.api.forecast_results import ForecastResultsAPIView
from gbm_apis.api.health import HealthCheckView
from gbm_apis.api.hier_metadata import HierarchyMetadataAPIView

app_name = 'gbm_apis'


## TODO: Health Check Implementation
## TODO: TraceId Propagation- Middleware
urlpatterns = [
    path('v2/drilldown_fields/', DrilldownFieldsV2.as_view(), name='drilldown_fields_v2'),
    re_path('basic_results/?$', DataLoadAPIView.as_view(), name='basic_results'),
    path('deals_results/', DealsResultsAPIView.as_view(), name='deals_results'),
    # No trailing slash required: the jobs call /gbm/forecast_results?period=... (as legacy), and a
    # slash-append redirect would be an extra hop for them.
    re_path(r'^forecast_results/?$', ForecastResultsAPIView.as_view(), name='forecast_results'),
    # GET only (read-only checksum). The SDK calls /gbm/hier_metadata without a trailing slash.
    re_path(r'^hier_metadata/?$', HierarchyMetadataAPIView.as_view(), name='hier_metadata'),
    path('health/', HealthCheckView.as_view(), name='health'),
]