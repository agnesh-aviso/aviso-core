"""Period boundary APIs, ported from service-infrastructure/aviso/api/datemanager.py.

The SDK reaches these through ``shell.current_period()``, ``shell.prev_period()``
and ``shell.next_period()``. All three are needed even when a caller only wants
the current period: ``PeriodInfo.initialize()`` in gbm_apis/interface/
shellDateUtils.py builds its period cache from current + prev + next in one go,
so the first current_period() call fans out to every one of these endpoints.

Legacy serves these at /date/<action>; this app is mounted at /gbm/, so the SDK
asks for /gbm/date/<action>. ShiftDate and TenantDateStrToEpoch are not ported --
/date/shift is commented out in legacy's urls.py too, and nothing in the
registered shell methods reaches the latter.
"""
import logging

from aviso.utils import GnanaError
from aviso.utils.dateUtils import (current_period, datetime2epoch,
                                   epoch2datetime, next_period, prev_period)

from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin

logger = logging.getLogger('gnana.%s' % __name__)


class DateView(AvisoCompatibilityMixin, AvisoView):
    """Shared parameter parsing and serialisation for the period endpoints."""

    http_method_names = ['get']

    restrict_to_roles = AvisoView.Role.All_Roles

    def return_data(self, current):
        """Serialise dateUtils' (mnemonic, begin, end) tuples as JSON.

        current_period() returns a single tuple while prev/next return a list
        of them, so the str check on element 0 tells the two apart. Either way
        the result is a list -- the SDK's current_period_ indexes [0].
        """
        if isinstance(current[0], str):
            return [{
                'mnemonic': current[0],
                'begin': datetime2epoch(current[1]),
                'end': datetime2epoch(current[2])
            }]
        else:
            ret_value = []
            for x in current:
                ret_value.append({
                    'mnemonic': x[0],
                    'begin': datetime2epoch(x[1]),
                    'end': datetime2epoch(x[2])
                })
            return ret_value

    def get_params(self, request):
        as_of = request.GET.get('as_of', None)
        if as_of:
            as_of = epoch2datetime(int(as_of))
        period_type = request.GET.get('type', 'Q')
        if period_type not in ('Q', 'Y', 'M', 'CM', 'W'):
            raise GnanaError("Unknown period type")
        return as_of, period_type

    def get_skip_and_count(self, request):
        return (int(request.GET.get('skip', 1)),
                int(request.GET.get('count', 1)))


class CurrentPeriod(DateView):

    def get(self, request, *args, **kwargs):
        as_of, period_type = self.get_params(request)
        current = current_period(as_of, period_type)
        return self.return_data(current)


class NextPeriod(DateView):

    def get(self, request, *args, **kwargs):
        as_of, period_type = self.get_params(request)
        skip, count = self.get_skip_and_count(request)
        current = next_period(as_of, period_type, skip=skip, count=count)
        return self.return_data(current)


class PrevPeriod(DateView):

    def get(self, request, *args, **kwargs):
        as_of, period_type = self.get_params(request)
        skip, count = self.get_skip_and_count(request)
        current = prev_period(as_of, period_type, skip=skip, count=count)
        return self.return_data(current)
