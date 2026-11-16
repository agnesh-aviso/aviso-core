"""Combined-results retrieval for ``/gbm/forecast_results``.

Ported from legacy gbm-service ``gbm/utils/resultUtils.py`` (``retrieve_combined_results`` and its
helpers), ``gbm/domainmodel/datameta.py`` (``TargetDOClass``) and service-infrastructure
``aviso/framework/views.py`` (``Separator``). Function bodies are verbatim; the only changes are:

* ``TargetSpec.getFullByName(target)`` -> ``TargetSpec.getFullByName(target, gbm_config)``: the target
  spec is read from the GBM microservice Postgres, as legacy gbm-service reads it (see ``gbm_config``).
* ``gnana_db.findAllDocuments(...)`` in ``get_results`` -> ``_find_gbm_docs(...)``: the same query on the
  tenant's GBM Mongo DB through a ``ConnectionFactory`` cursor, the deals_results pattern, with the same
  ``_encdata`` decryption ``gnana_db`` applies. The branches this endpoint never reaches (``distinct``,
  ``raw_data``) are left untouched and still use ``gnana_db``, which resolves to the same DB.
* ``gbm_config`` is threaded through as a parameter.

Read-only: nothing in this module writes to Mongo or Postgres.
"""
import collections
import datetime
import itertools
import json
import logging
import os
import sys
import traceback
from copy import deepcopy  # noqa: F401  (report_spec eval scope, see below)
from collections import defaultdict  # noqa: F401  (report_spec eval scope)

import numpy as np  # noqa: F401  (report_spec eval scope)
import pymongo
import pytz
from bson import BSON
from django.http.request import QueryDict
from django.http.response import HttpResponseNotFound, HttpResponseBadRequest, HttpResponseBase
from django.utils.html import escape

from aviso.framework.connection_factory import ConnectionFactory
from aviso.settings import sec_context, gnana_db
from aviso.utils import GnanaError, dateUtils
from aviso.utils.misc_utils import try_float  # noqa: F401  (report_spec eval scope)
from aviso.utils.dateUtils import datetime2epoch, current_period, prev_period, period_details2, \
    epoch2datetime, next_period, TimeHorizon, epoch, get_all_periods, period_details_by_mnemonic, \
    xl2datetime, datetime2xl, get_prevq_mnem  # noqa: F401  (the unused ones: report_spec eval scope)

from gbm_apis.deal_result.dataset import get_time_horizon
from gbm_apis.deal_result.results import DimensionResult
from gbm_apis.forecast_result.gbm_config import TargetSpec

logger = logging.getLogger('gnana.%s' % __name__)

# Legacy gbm/__init__.py py2 shims used by the ported code.
basestring = str
long = int

# NOTE on imports marked "report_spec eval scope": retrieve_combined_results builds each report_spec
# entry with eval('lambda v:' + expr), and eval resolves names against THIS module's globals. Legacy
# evaluated them in resultUtils' globals, so the same utility names are kept importable here so a
# tenant expression that uses one of them (e.g. try_float) behaves the same.

# Batch size for the GBM Mongo cursor, same as the deals_results flow.
GBM_CURSOR_BATCH_SIZE = 500


def _find_gbm_docs(collection_name, criteria, sort=None):
    """``gnana_db.findAllDocuments(collection_name, criteria, sort=sort)`` on a ConnectionFactory cursor.

    Same DB as ``gnana_db`` (the tenant's GBM Mongo DB, ``db_type='gbm'``) and the same per-document
    decryption as aviso ``MongoDBCursorIterator``. Read-only: ``find`` only. ``no_cursor_timeout``
    cursors never expire server-side, so the cursor is always closed here, also when the caller stops
    early or raises.
    """
    cname = os.environ.get("CNAME", "preprod")
    gbm_db = ConnectionFactory.get_mongo_db(tenant=sec_context.name, db_type='gbm', cname=cname)
    cursor = gbm_db[collection_name].find(criteria, sort=sort, batch_size=GBM_CURSOR_BATCH_SIZE,
                                          no_cursor_timeout=True)
    try:
        for doc in cursor:
            if '_encdata' in doc:
                doc['object'] = BSON(sec_context.decompress(doc.pop('_encdata'))).decode()
            yield doc
    finally:
        cursor.close()


class Separator(object):

    def __init__(self, sep=','):
        self.first = True
        self.sep = sep

    def separator(self):
        if self.first:
            self.first = False
            return ''
        else:
            return self.sep


def TargetDOClass(target_name, target_key, sandbox=None):
    class TargetDOClassToUse(DimensionResult):
        collection_name = 'combined_results.' + target_name + '.' + target_key
        if sandbox:
            collection_name += '.' + sandbox
        execution_time = 'N/A'
    return TargetDOClassToUse


distinct_key_map = {
    'as_of': 'as_of',
    'cache_key': 'run_time_horizon',
    'keys': 'run_time_horizon',
    'mnemonic': 'as_of_mnemonic'
}


def get_filter_criteria(inputs):
    criteria1 = []
    criteria2 = []
    mnemonic_criteria = []

    if 'mnemonic' in inputs:
        mnemonics = inputs.getlist('mnemonic')
        if mnemonics:
            if 'ALL' in mnemonics:
                mnemonic_criteria.append({})
            else:
                if len(mnemonics) > 1:
                    mnemonic_criteria.append({'$or': [{'object.as_of_mnemonic': x}
                                                      for x in mnemonics]})
                else:
                    mnemonic_criteria.append({'object.as_of_mnemonic': mnemonics[0]})

    for fld_name in ['begin_month_mnemonic', 'as_of_month_mnemonic', 'horizon_month_mnemonic']:
        month_mnem = inputs.getlist(fld_name)
        if month_mnem:
            if len(month_mnem) > 1:
                mnemonic_criteria.append({'$or': [{'object.%s' % fld_name: int(x)} for x in month_mnem]})
            else:
                mnemonic_criteria.append({'object.%s' % fld_name: int(month_mnem[0])})

    if mnemonic_criteria:
        if len(mnemonic_criteria) > 1:
            criteria1.append({'$and': mnemonic_criteria})
        elif len(mnemonic_criteria):
            criteria1 = mnemonic_criteria

    filter_ex = inputs.get('filter', None)
    if filter_ex:
        position = filter_ex.find('~::~')
        if position == -1:
            return HttpResponseBadRequest('Invalid value for filter')
        criteria2.append({'object.dim_attrs.dimensions': filter_ex})

    as_of_tuples = inputs.get('as_of_tuple')
    if as_of_tuples:
        as_of_tuples = json.loads(as_of_tuples)
        if not isinstance(as_of_tuples[0], list):
            as_of_tuples = [as_of_tuples]
        as_of_tuple_criteria = []
        for as_of_tuple in as_of_tuples:
            cache_key, as_of, begins, horizon = as_of_tuple
            cache_key = cache_key or \
                get_time_horizon(as_of=as_of,
                                 begins=begins,
                                 horizon=horizon).encode_time_horizon()
            run_time_horizon = 'custom_%s' % cache_key
            as_of_tuple_criteria.append({'object.run_time_horizon': run_time_horizon})
        criteria1.append({'$or': as_of_tuple_criteria})

    fcst_type = inputs.get('fcst_type')
    if fcst_type:
        if fcst_type.endswith('eoq_fcst'):
            # assume 10 years backward and 2 years forward is all the periods
            # we are interested in
            q_infos = period_details2(period_type='Q', delta=8, count=-40)
            fcst_crit = []
            for i, q_info in enumerate(q_infos[:-1]):
                boq = q_info[1]
                eoq = q_info[2].replace(microsecond=0)
                if fcst_type.startswith('next_'):
                    eoq = q_infos[i + 1][2].replace(microsecond=0)

                fcst_crit.append({
                    '$and': [
                        {'object.begins': boq.astimezone(pytz.utc)},
                        {'object.horizon': eoq.astimezone(pytz.utc)},
                    ]})

            criteria1 = [{'$or': fcst_crit}]
        elif fcst_type.endswith('exp_fcst'):
            try:
                as_of = inputs.get('as_of')
                as_of = epoch2datetime(long(as_of))
            except:
                raise Exception(
                    "ERROR: /combined_results/retrieve fcst_type=exp_fcst requires valid as_of to be passed in")
            if fcst_type.startswith('next_'):
                q_info = next_period(as_of)
                min_horizon = q_info[1]  # next boq
                max_horizon = q_info[2]  # next eoq
            else:
                q_info = current_period(as_of)
                min_horizon = as_of
                max_horizon = q_info[2]  # this boq
            criteria1.append({
                '$and': [
                    {'object.as_of': as_of.astimezone(pytz.utc)},
                    {'object.horizon': {
                        '$gte': min_horizon.astimezone(pytz.utc)}},
                    {'object.horizon': {
                        '$lte': max_horizon.astimezone(pytz.utc)}},
                ]})
        else:
            logger.warn("Unknown fcst_type='%s' passed to /combine_results/retrieve" % fcst_type)
    for x in ('as_of', 'begins', 'horizon'):
        x_val = inputs.get(x)
        if x_val:
            x_val = epoch2datetime(int(x_val))
            if x_val.tzinfo:
                x_val = x_val.astimezone(pytz.utc)
            criteria1.append({('object.%s' % x): x_val})
    if len(criteria1) == 0 and len(criteria2) == 0:
        return [], []
    else:
        return criteria1, criteria2


def always_true(*args, **kwargs):
    return True


def retrieve_combined_results(inputs, target, target_key, permission_checker, sandbox=None,
                              gbm_config=None):
    report_spec_name = inputs.get('report_spec', None)
    requested = []
    distinct_vals = inputs.getlist('distinct', [])
    group_bys = inputs.getlist('groupby', [])
    raw_data = inputs.getlist('raw_data', [])
    field_list = set(inputs.getlist('fields', []))
    dateStrings = inputs.get('dateStrings')
    # true when date strings of as_of, begins and horizon date are needed
    if distinct_vals:
        requested.append('distinct')
    if group_bys:
        requested.append('groupby')
        if not report_spec_name:
            return HttpResponseBadRequest('report_spec is mandatory with groupby')
    if raw_data:
        requested.append('raw_data')

    if len(requested) == 0:
        return HttpResponseBadRequest('Nothing is requested')
    elif len(requested) > 1:
        return HttpResponseBadRequest('Only one of %s is allowed' % " and ".join(requested))

    TargetDO = TargetDOClass(target, target_key, sandbox)
    TargetDOLive = None

    if inputs.get('add_live'):
        TargetDOLive = TargetDOClass(inputs.get('add_live'), target_key)

    time_criteria, dim_criteria = get_filter_criteria(inputs)

    if time_criteria or dim_criteria:
        criteria = {"$and": time_criteria + dim_criteria}
    else:
        criteria = {}
    target_spec = TargetSpec.getFullByName(target, gbm_config)

    if not target_spec:
        return HttpResponseNotFound('No such target spec')

    weekly_dimensions = target_spec.drilldowns
    for x in distinct_vals + group_bys + raw_data:
        if x in distinct_key_map or x in ('.', 'as_of_tuples'):
            continue

        for y in weekly_dimensions:
            if y.startswith(x):
                break
        else:
            raise GnanaError('Given grouping value %s is not configured' % escape(x))

    if report_spec_name:
        target_spec = TargetSpec.getFullByName(target, gbm_config)
        if not target_spec or report_spec_name not in target_spec.report_spec:
            return HttpResponseBadRequest('Unable to find report spec')
        report_spec = target_spec.report_spec[report_spec_name]
        fields_to_return = field_list or report_spec
    else:
        report_spec = None

    if report_spec:
        report_spec_functions = dict((k, eval('lambda v:' + v)) for k, v in report_spec.items())

    s = Separator(',\n')

    def yield_results(w, yield_fn):
        yield '{'
        for x in w:
            yield s.separator()
            yield '%s:' % json.dumps(x)
            for v1 in yield_fn(x):
                yield v1
        yield '}'

    def yield_distinct_vals(x):
        yield '['
        s1 = Separator()
        if x not in ('as_of', 'cache_key', 'keys', 'mnemonic', 'as_of_tuples'):
            criteria_2 = {'$and': [criteria, {'object.dim_attrs.full_dim_type': x}]}
        else:
            criteria_2 = criteria
        if x in distinct_key_map:
            all_results = gnana_db.getDistinctValues(TargetDO.getCollectionName(),
                                                     'object.%s' % distinct_key_map[x],
                                                     criteria_2)
        elif x == 'as_of_tuples':
            match_criteria = criteria_2
            groupby_criteria = {'run_time_horizon': "$object.run_time_horizon",
                                'begins': "$object.begins",
                                'as_of': "$object.as_of",
                                'horizon': "$object.horizon",
                                }
            pipeline = [{'$match': match_criteria},
                        {"$group": {"_id": groupby_criteria}}]
            result = gnana_db.getAggregateValues(TargetDO.getCollectionName(), pipeline)
            all_results = []
            for y in result:
                res = y.pop('_id')
                record = res
                all_results.append(record)
            from operator import itemgetter
            all_results = sorted(all_results, key=itemgetter('run_time_horizon'))
        else:
            all_results = gnana_db.findAllDocuments(TargetDO.getCollectionName(), criteria_2)

        distinct_results = set()
        for y in all_results:
            if x in distinct_key_map:
                if isinstance(y, datetime.datetime):
                    val = datetime2epoch(dateUtils.from_db(y))
                else:
                    val = y
            elif x == 'as_of_tuples':
                run_time_horizon = y['run_time_horizon']
                val = (
                    run_time_horizon[7:] if run_time_horizon.startswith('custom_') else None,
                    datetime2epoch(dateUtils.from_db(y['begins'])),
                    datetime2epoch(dateUtils.from_db(y['as_of'])),
                    datetime2epoch(dateUtils.from_db(y['horizon'])),
                )
                if dateStrings:
                    tempHolder = (
                        epoch2datetime(datetime2epoch(
                            (dateUtils.from_db(
                                y['begins'])))).isoformat(),
                        epoch2datetime(datetime2epoch(
                            (dateUtils.from_db(
                                y['as_of'])))).isoformat(),
                        epoch2datetime(datetime2epoch(
                            (dateUtils.from_db(
                                y['horizon'])))).isoformat(), )
                    val = val + tempHolder
            else:
                if not permission_checker(y['object']['dim_attrs']['full_dim_type'],
                                          y['object']['dim_attrs']['full_dim_val']):
                    continue
                val = y['object']['dim_attrs']['full_dim_val']
            if val not in distinct_results:
                yield '%s\n %s' % (s1.separator(), json.dumps(val))
                distinct_results.add(val)
        yield ']'

    def get_results(x, s, ignore_dim_criteria=False):
        criteria_2 = {
            '$and': [{'object.dim_attrs.full_dim_type': x}]
            + time_criteria
            + (dim_criteria if not ignore_dim_criteria else [])
        }

        all_results = _find_gbm_docs(TargetDO.getCollectionName(), criteria_2, sort=s)

        if TargetDOLive:
            all_results_live = _find_gbm_docs(TargetDOLive.getCollectionName(), criteria_2, sort=s)
            consolidated_results = list(itertools.chain(all_results, all_results_live))
            # Combined results from a target report and RTFM counterpart need to be ordered based
            # on combination of following criteria
            # dim_attrs.full_dim_val - DESC, begins - ASC, as_of - ASC, horizon - ASC
            # For achieving this in memory, we have to sort the list TWICE
            sorted_consolidated_results = sorted(consolidated_results, key=lambda e: (e['object']['begins'],
                                                                                      e['object']['as_of'],
                                                                                      e['object']['horizon']))
            sorted_consolidated_results = sorted(sorted_consolidated_results,
                                                 key=lambda e: (e['object']['dim_attrs']['full_dim_val']),
                                                 reverse=True)
            all_results = sorted_consolidated_results

        for y in all_results:
            if not ignore_dim_criteria and not permission_checker(y['object']['dim_attrs']['full_dim_type'],
                                                                  y['object']['dim_attrs']['full_dim_val']):
                continue
            yield (
                y['object']['dim_attrs']['final_dim_val'],
                y['object']['dim_attrs']['dimensions'],
                {
                    'as_of': datetime2epoch(dateUtils.from_db(y['object']['as_of'])),
                    'begins': datetime2epoch(dateUtils.from_db(y['object']['begins'])),
                    'horizon': datetime2epoch(dateUtils.from_db(y['object']['horizon'])),
                    'mnemonic': y['object']['as_of_mnemonic'],
                    'cache_key': y['object']['run_time_horizon'][7:] if y['object']['run_time_horizon'].startswith('custom_') else None
                },
                y['object']['extended_attrs']
            )

    def yield_raw_values(x):
        s1 = Separator()
        yield '['
        results = get_results(x, [('object.dim_attrs.full_dim_val', pymongo.DESCENDING)])
        for v in results:
            yield s1.separator()
            yield json.dumps(v)
        yield ']'

    def uniform_iterator(all_results, uniform_dates, terminal_value=None):
        uniform_iter = iter(uniform_dates)
        uniform_date_set = set(str(x[2]) for x in uniform_dates)
        prev_result = None
        while True:
            try:
                a_result = next(all_results)
                if str(a_result[2]) not in uniform_date_set:
                    continue
            except StopIteration:
                if prev_result is not None:
                    for next_uniform_result in uniform_iter:
                        yield (prev_result[0], prev_result[1], next_uniform_result[2], {})
                break

            try:
                next_uniform_result = next(uniform_iter)
            except StopIteration:
                if prev_result is None:
                    prev_result = a_result
                if a_result[1] == prev_result[1]:
                    if prev_result is None:
                        prev_result = a_result
                    while a_result[1] == prev_result[1]:
                        a_result = next(all_results)

                # Reset the uniform results for the next set
                uniform_iter = iter(uniform_dates)
                next_uniform_result = next(uniform_iter)
                prev_result = a_result

            if prev_result and prev_result[1] != a_result[1]:
                # We moved on to the next set.  Time to make sure all the uniform results are produced
                for next_uniform_result in itertools.chain([next_uniform_result], uniform_iter):
                    yield (prev_result[0], prev_result[1], next_uniform_result[2], {})

                # Reset the uniform results for the next set
                uniform_iter = iter(uniform_dates)
                next_uniform_result = next(uniform_iter)
                prev_result = a_result

            # Gotcha - If something comes in a_result that doesn't exist in uniform results
            # This will go into infinite loop
            while next_uniform_result[2] != a_result[2]:
                if prev_result is None:
                    prev_result = a_result
                yield (prev_result[0], prev_result[1], next_uniform_result[2], {})
                try:
                    next_uniform_result = next(uniform_iter)
                except StopIteration:
                    uniform_iter = iter(uniform_dates)
                    next_uniform_result = next(uniform_iter)
                    prev_result = a_result

            yield a_result
            prev_result = a_result

        if terminal_value is not None:
            yield terminal_value

    def yield_group_values(x):
        prev_value = None
        start_pos = len(x) + 4
        yield '{'
        s1 = Separator(',\n')
        d = collections.defaultdict(list)

        summary_results = [v for v in get_results(".", [
            ('object.dim_attrs.full_dim_val', pymongo.DESCENDING),
            ('object.begins', pymongo.ASCENDING),
            ('object.as_of', pymongo.ASCENDING),
            ('object.horizon', pymongo.ASCENDING),
        ], ignore_dim_criteria=True)]

        results = get_results(x, [
            ('object.dim_attrs.full_dim_val', pymongo.DESCENDING),
            ('object.begins', pymongo.ASCENDING),
            ('object.as_of', pymongo.ASCENDING),
            ('object.horizon', pymongo.ASCENDING),
        ])

        for v in uniform_iterator(results, summary_results,
                                  terminal_value=('', [''], {'as_of': 0,
                                                             'begins': 0,
                                                             'horizon': 0,
                                                             'mnemonic': '',
                                                             'cache_key': ''}, {})):
            cur_value = v[1][-1]
            if prev_value != cur_value:
                if d:
                    yield s1.separator()
                    yield '%s:%s' % (json.dumps(prev_value[start_pos:]), json.dumps(d))
                    d = collections.defaultdict(list)

            prev_value = cur_value
            for f in fields_to_return:
                if f in report_spec:
                    d[f].append(report_spec_functions[f](v))

        yield '}'

    if distinct_vals:
        r, g = distinct_vals, yield_distinct_vals
    elif raw_data:
        r, g = raw_data, yield_raw_values
    elif group_bys:
        r, g = group_bys, yield_group_values
    else:
        raise GnanaError("Don't know what is requested")
    return yield_results(r, g)


def retrieve_combined_results_helper(params=None, target=None, target_key=None, sandbox=None,
                                     gbm_config=None):
    """Helper function for retrieving combined results used by bookings cache task.
    The only purpose of this function is to turn the params dict into a QueryDict
    (since that is what the CR results util needs) and to do better error handling.

    TODO: Obviate this function by having combined results API turn its QueryDict into
    a real dict and have the backend result util use dicts only and have good error messages.
    """
    inputs = QueryDict('', mutable=True)

    for k, v in params.items():
        if isinstance(v, list):
            inputs.setlist(k, v)
        else:
            inputs[k] = v
    logger.info("retrieving combined results for target %s", target)
    try:
        resp_gen = retrieve_combined_results(
            inputs, target, target_key, always_true, sandbox, gbm_config=gbm_config)
        if isinstance(resp_gen, HttpResponseBase):
            return {"error": resp_gen.content.decode()}
        resp_string = ''.join(resp_gen)
        logger.info("returned combined results for target %s", target)
        return json.loads(resp_string)
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        err_msg = "\n".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        message = "\n".join(
            [
                "ERROR: Failed to load combined results for target: %s, target_key: %s, params: %s" % (
                    target, target_key, params),
                str(e), err_msg,
            ])
        raise GnanaError(message)
