from django.urls import path, re_path
from gbm_apis.api.drilldown_fields_v2 import DrilldownFieldsV2
from gbm_apis.api.data_load import DataLoadAPIView
from gbm_apis.api.date_manager import CurrentPeriod, NextPeriod, PrevPeriod
from gbm_apis.api.deals_results import DealsResultsAPIView
from gbm_apis.api.health import HealthCheckView
from gbm_apis.api.sandbox_manager import CommitSandbox, ListSandbox
from gbm_apis.api.tenant_config import TenantConfig, TenantConfigDump
from gbm_apis.dataset.datasetdesigner import DatasetUpdate, DatasetView

app_name = 'gbm_apis'


## TODO: Health Check Implementation
## TODO: TraceId Propagation- Middleware
urlpatterns = [
    path('v2/drilldown_fields/', DrilldownFieldsV2.as_view(), name='drilldown_fields_v2'),
    re_path('basic_results/?$', DataLoadAPIView.as_view(), name='basic_results'),
    path('deals_results/', DealsResultsAPIView.as_view(), name='deals_results'),
    path('health/', HealthCheckView.as_view(), name='health'),
    re_path(r'^dataset/(?P<dataset>\w+)/(?P<sandbox>\w+)/(?P<action>\w+)$',
            DatasetUpdate.as_view(), name='dataset_update'),
    re_path(r'^dataset/(?P<dataset>\w+)/(?P<sandbox>\w+)$',
            DatasetView.as_view(), name='dataset_sandbox'),
    re_path(r'^dataset/(?P<dataset>\w+)$',
            DatasetView.as_view(), name='dataset'),
    re_path(r'^sandbox/list$', ListSandbox.as_view(), name='sandbox_list'),
    re_path(r'^sandbox/commit$', CommitSandbox.as_view(), name='sandbox_commit'),
    # Legacy spells this /tenant/<name>/config; this app is mounted at /gbm/,
    # so the SDK asks for /gbm/tenant/<name>/config.
    re_path(r'^tenant/(?P<tenant_name>[\w.]+)/config$',
            TenantConfigDump.as_view(), name='tenant_config'),
    # One entry of that config: GET reads it, POST saves or removes it.
    re_path(r'^tenant/(?P<tenant_name>[\w.]+)/config/(?P<category>[\w.]+)/(?P<config_name>[\w.]+)$',
            TenantConfig.as_view(), name='tenant_config_item'),
    # Period boundaries. All three are registered together because the SDK's
    # PeriodInfo cache is built from current + prev + next on the first call,
    # so current_period() alone still reaches /date/next.
    re_path(r'^date/current$', CurrentPeriod.as_view(), name='date_current'),
    re_path(r'^date/next$', NextPeriod.as_view(), name='date_next'),
    re_path(r'^date/prev$', PrevPeriod.as_view(), name='date_prev'),
]
