import json
import logging
from django.http.response import StreamingHttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from aviso.utils import is_none, is_true
from gbm_apis.deal_result.result_Utils import deals_results_by_period, deals_results_by_timestamp
from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin

logger = logging.getLogger('gnana.%s' % __name__)


def iter_deal_results_json(value):
    """Serialize one period's (or timestamp's) value, streaming its deals.

    Carries the same data as ``json.dumps(value)``; what differs is only
    insignificant whitespace and the position of ``results``, which moves to the
    end of the object. The point is that no string larger than a single deal is
    ever built -- the ``json.dumps`` call this replaces held the whole object
    graph *and* a full serialized copy of it at the same time.

    Each deal still goes through ``json.dumps``, so serialization stays on the C
    encoder: 26.7s against the single call's 23.0s on a 3.8GB tenant payload.
    ``JSONEncoder().iterencode`` also avoids the big string, but its public form
    runs the pure-Python encoder (only ``dumps``/``encode`` take the C path) and
    measured 105.8s on the same data.
    """
    # File-list modes return a plain list -- small payload, serialize as before.
    # The isinstance check on results also stops an unexpected shape from
    # raising once the first byte is out, when the 200 and headers are already
    # on the wire and the client can only see a truncated body.
    deal_results = value.get('results') if isinstance(value, dict) else None
    if not isinstance(deal_results, dict):
        yield '%s\n' % json.dumps(value)
        return

    # Stream deal results one at a time to avoid a single giant json.dumps string
    yield '{\n'
    for key, val in value.items():
        if key == 'results':
            continue
        yield '%s: %s,\n' % (json.dumps(str(key)), json.dumps(val))
    yield '"results": {\n'
    for i, (deal_id, deal_data) in enumerate(deal_results.items()):
        if i:
            yield ','
        # str(): json.dumps coerces non-string keys, and without it a
        # non-string id emits `1: {...}`, which is not valid JSON.
        yield '%s: %s\n' % (json.dumps(str(deal_id)), json.dumps(deal_data))
    yield '}\n'
    yield '}\n'


@method_decorator(csrf_exempt, name='dispatch')
class DealsResultsAPIView(AvisoCompatibilityMixin, AvisoView):
    """
    API View to stream deals results.
    Refactored to use AvisoView + Compatibility Mixin for auth & stability.
    """

    # --- Configuration ---
    http_method_names = ['get', 'post']
    restrict_to_roles = {AvisoView.Role.Gnacker}

    # CRITICAL: We return StreamingHttpResponse, so we must disable
    # AvisoView's attempt to wrap responses in JSON.
    as_json = False

    def get(self, request, *args, **kwargs):
        periods = request.GET.getlist('period', [])
        timestamps = [int(x) for x in request.GET.getlist('timestamp', [])]
        get_results_from_as_of = int(request.GET.get('get_results_from_as_of', 0))
        node = request.GET.get('node')

        include_uip = is_true(request.GET.get('include_uip', True))
        force_uip_and_hierarchy = is_true(request.GET.get('force_uip_and_hierarchy', False))
        allow_live = is_true(request.GET.get('allow_live', True))
        return_files_list = is_true(request.GET.get('return_files_list', False))
        return_chipotle_files_list = is_true(request.GET.get('return_chipotle_files_list', False))

        fields = request.GET.getlist('fields', [])

        if not timestamps:
            return StreamingHttpResponse(
                self.yield_period_results(periods, include_uip, node,
                                          get_results_from_as_of=get_results_from_as_of,
                                          fields=fields,
                                          return_files_list=return_files_list,
                                          return_chipotle_files_list=return_chipotle_files_list),
                status=200, content_type='application/json')
        elif len(periods) == 1:
            return StreamingHttpResponse(
                self.yield_timestamp_results(periods[0], timestamps, include_uip, node,
                                             get_results_from_as_of=get_results_from_as_of, fields=fields,
                                             force_uip_and_hierarchy=force_uip_and_hierarchy, allow_live=allow_live,
                                             return_files_list=return_files_list,
                                             return_chipotle_files_list=return_chipotle_files_list),
                status=200, content_type='application/json')
        else:
            raise Exception("Invalid Request: Multiple periods provided with timestamps.")

    def post(self, request, *args, **kwargs):
        # 1. Parse Parameters
        periods = request.GET.getlist('period', [])

        try:
            # Use raw body decoding for safety
            raw_body = request.body.decode('utf-8') if hasattr(request, 'body') else ''
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {}

        fields = []
        if not is_none(body.get('fields', None)):
            fields = body.get('fields', None)

        opp_ids = []
        if not is_none(body.get('opp_ids', None)):
            opp_ids = body.get('opp_ids', None)

        get_results_from_as_of = int(request.GET.get('get_results_from_as_of', 0))
        timestamps = [int(x) for x in (request.GET.getlist('timestamp', []))]
        node = request.GET.get('node')
        include_uip = is_true(request.GET.get('include_uip', True))
        force_uip_and_hierarchy = is_true(request.GET.get('force_uip_and_hierarchy', False))
        allow_live = is_true(request.GET.get('allow_live', True))
        return_files_list = is_true(request.GET.get('return_files_list', False))

        if not timestamps:
            return StreamingHttpResponse(
                self.yield_period_results(periods, include_uip, node,
                                          get_results_from_as_of=get_results_from_as_of,
                                          fields=fields, opp_ids=opp_ids,
                                          return_files_list=return_files_list),
                status=200, content_type='application/json')
        elif len(periods) == 1:
            return StreamingHttpResponse(
                self.yield_timestamp_results(periods[0], timestamps, include_uip, node,
                                             get_results_from_as_of=get_results_from_as_of, fields=fields,
                                             opp_ids=opp_ids, force_uip_and_hierarchy=force_uip_and_hierarchy,
                                             allow_live=allow_live, return_files_list=return_files_list),
                status=200, content_type='application/json')
        else:
            raise Exception("Invalid Request: Multiple periods provided with timestamps.")

    # --- Helper Generators (Logic kept as-is) ---

    def yield_period_results(self, periods, include_uip, node, get_results_from_as_of=0, fields=[], opp_ids=[],
                             return_files_list=False, return_chipotle_files_list=False):
        yield '{\n'
        for x, period in enumerate(periods):
            if x:
                yield ','
            yield '%s:\n' % json.dumps(period)
            period_data = deals_results_by_period([period], include_uip=include_uip, node=node,
                                                  get_results_from_as_of=get_results_from_as_of,
                                                  fields=fields,
                                                  return_files_list=return_files_list,
                                                  return_chipotle_files_list=return_chipotle_files_list)[period]
            yield from iter_deal_results_json(period_data)
            # Release before the next period is fetched.
            del period_data
        yield '}\n'

    def yield_timestamp_results(self, period, timestamps, include_uip, node, get_results_from_as_of=0, fields=[],
                                opp_ids=[], force_uip_and_hierarchy=False, allow_live=True, return_files_list=False,
                                return_chipotle_files_list=False):
        changed_deals = []
        yield '{\n'
        for x, timestamp in enumerate(timestamps):
            if x:
                yield ','
            yield '%s:\n' % json.dumps(str(timestamp))
            timestamp_data = deals_results_by_timestamp(period, [timestamp], include_uip=include_uip,
                                                        node=node,
                                                        get_results_from_as_of=get_results_from_as_of,
                                                        fields=fields, opp_ids=opp_ids,
                                                        changed_deals=changed_deals,
                                                        force_uip_and_hierarchy=force_uip_and_hierarchy,
                                                        allow_live=allow_live,
                                                        return_files_list=return_files_list,
                                                        return_chipotle_files_list=return_chipotle_files_list)[timestamp]
            yield from iter_deal_results_json(timestamp_data)
            # Release before the next timestamp is fetched.
            del timestamp_data
        yield '}\n'