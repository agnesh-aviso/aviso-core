"""``/gbm/forecast_results`` business logic.

Ported from legacy gbm-service ``gbm/utils/resultUtils.py``: ``gimme_forecast_results``,
``create_cr_params`` and the segment-forecast helpers. Function bodies are verbatim; the only changes are:

* ``Dataset.getByNameAndStage(...)`` -> ``get_dataset(name, gbm_config)``,
  ``TargetSpec.getFullByName(target)`` -> ``TargetSpec.getFullByName(target, gbm_config)`` and
  ``sec_context.details.get_config('forecast', 'tenant')`` -> ``gbm_config.get_config(...)``: these
  configs are read from the GBM microservice Postgres, which is where legacy gbm-service reads them.
* ``gbm_config`` is threaded through as a parameter.

The period calendar (``janky_period_parser``, ``period_details_by_mnemonic``) is read the same way as
the other aviso-core endpoints (deals_results).

Read-only: nothing in this module writes to Mongo or Postgres.
"""
import datetime
import logging
from copy import deepcopy

import numpy as np
from aviso.utils.dateUtils import period_details_by_mnemonic

from gbm_apis.deal_result.result_Utils import janky_period_parser
from gbm_apis.forecast_result.combined_results import retrieve_combined_results_helper
from gbm_apis.forecast_result.gbm_config import TargetSpec, get_dataset

logger = logging.getLogger('gnana.%s' % __name__)


def list_add(a, b):
    return [i+j for i, j in zip(a, b)]


def list_subs(a, b):
    return [i-j for i, j in zip(a, b)]


def list_mult(a, b):
    return [i*j for i, j in zip(a, b)]


def list_dev(a, b, fallback_value=0.0):
    return [float(i)/j if j != 0.0 else fallback_value for i, j in zip(a, b)]


def divide_Matrix_by_vec(mat, vec):
    m = len(mat)
    n = len(vec)
    for i in range(m):
        for j in range(n):
            if vec[j] != 0:
                mat[i][j] /= vec[j]
    return mat


def new_calibrate(node, segment_open_pipe_vals, segment_total_open_pipe_vals, ns_config):
    root_node = node.split('#')[0] + '#!'
    for k in segment_open_pipe_vals:
        if k.split('_')[-1] not in ns_config[root_node]:
            segment_open_pipe_vals[k] = [0]*len(segment_open_pipe_vals[k])
        segment_open_pipe_vals[k] = list_dev(segment_open_pipe_vals[k], segment_total_open_pipe_vals['seg_open_pipe_amount_'+k.split('_')[-1]])
    return segment_open_pipe_vals


def calibrate_segment_ratios(seg_forecasts, node, segments_ratio, ns_config, churn_segment = None):

    if ns_config is None:
        return segments_ratio

    _segments_ratio = segments_ratio.copy()

    # segments_ratio = segments_ratio.copy()

    root_node = node.split('#')[0] + '#!'
    seg_for_node = ns_config.get('seg_for_node',None)
    for seg_forecast in seg_forecasts:
        #         print('seg_forecast:', seg_forecast)

        fcst_seg_ratio_dict = {fld: _segments_ratio[fld] for fld in _segments_ratio if fld.startswith(seg_forecast)}

        #         print('fcst_seg_ratio_dict: ', fcst_seg_ratio_dict)
        for sr in fcst_seg_ratio_dict:
            if seg_for_node == True:
                for seg_node in ns_config.keys():
                    if seg_node in node:
                        if sr.split('_')[-1] not in ns_config[seg_node]:
                            fcst_seg_ratio_dict[sr] = [0] * len(_segments_ratio[sr])
                            break
            elif sr.split('_')[-1] not in ns_config[root_node]:
                fcst_seg_ratio_dict[sr] = [0] * len(_segments_ratio[sr])

        keyList = list(fcst_seg_ratio_dict.keys())
        valueList = list(fcst_seg_ratio_dict.values())

        for key in keyList: del _segments_ratio[key]
        #         print ('deleted ', seg_forecast, ' keys')

        #         print('valueList: ', valueList)

        sumList = sum(np.array(valueList)).astype(float)
        valueList = divide_Matrix_by_vec(valueList, sumList)


        #This is to make churn negative and increasing the Aviso forecast proportionally for non-churn segments.
        if churn_segment:
            valueList = np.array(valueList).astype(float)
            churn_index = keyList.index(list(filter(lambda i: i.endswith(churn_segment), keyList))[0])
            churn_row = np.copy(valueList[churn_index])
            valueList[churn_index] = 0 * valueList[churn_index]
            churn_multiplier = sum(valueList).astype(float) + 2 * churn_row
            sumList = sum(valueList).astype(float)
            sumList = divide_Matrix_by_vec([sumList.tolist()], churn_multiplier.tolist())[0]
            valueList = divide_Matrix_by_vec(valueList, sumList)
            valueList[churn_index] = -1 * churn_row
            valueList.tolist()

        _segments_ratio.update(dict(zip(keyList, valueList)))

    return _segments_ratio


def get_seg_fcst_based_on_open_pipe(report_type, seg, node_fcst, seg_fcst, segments_total_open_pipe_ratio,  segments_open_pipe_ratio, segments_np_ep_ratios):
    store = deepcopy(seg_fcst)
    # store_node = deepcopy(node_fcst)
    if report_type in ['bookings', 'nextq']:

        node_fcst['total_fcst_only'] = list_subs(node_fcst['tot_won_and_fcst'], node_fcst['tot_won'])
        node_fcst['total_fcst_only_sans_ubf'] = list_subs(node_fcst['total_fcst_only'], node_fcst['ubf'])

        seg_fcst['total_fcst_only'] = list_mult(node_fcst['total_fcst_only'], segments_total_open_pipe_ratio['seg_open_pipe_amount_'+seg])
        seg_fcst['total_fcst_only_sans_ubf'] = list_subs(seg_fcst['total_fcst_only'], seg_fcst['ubf'])
        #
        seg_fcst['tot_won_and_fcst'] = list_add(seg_fcst['total_fcst_only'], seg_fcst['tot_won'])

        seg_fcst['ep_fcst_only'] = list_mult(seg_fcst['total_fcst_only_sans_ubf'], segments_np_ep_ratios['seg_ep_open_pipe_amount_'+seg])
        seg_fcst['np_fcst_only_sans_ubf'] = list_mult(seg_fcst['total_fcst_only_sans_ubf'], segments_np_ep_ratios['seg_np_open_pipe_amount_'+seg])

        seg_fcst['ep_won_and_fcst'] = list_add(seg_fcst['ep_won'], seg_fcst['ep_fcst_only'])
        seg_fcst['np_won_and_fcst_sans_ubf'] = list_add(seg_fcst['np_won'], seg_fcst['np_fcst_only_sans_ubf'])
        seg_fcst['np_won_and_fcst'] = list_add(seg_fcst['np_won_and_fcst_sans_ubf'], seg_fcst['ubf'])

        node_fcst['total_fcst_hi_only'] = list_subs(node_fcst['tot_won_and_fcst_hi'], node_fcst['tot_won'])
        seg_fcst['total_fcst_hi_only'] = list_mult(node_fcst['total_fcst_hi_only'],segments_total_open_pipe_ratio['seg_open_pipe_amount_' + seg])
        seg_fcst['total_fcst_hi_only_sans_ubf'] = list_subs(seg_fcst['total_fcst_hi_only'], seg_fcst['ubf'])
        seg_fcst['np_fcst_hi_only_sans_ubf'] = list_mult(seg_fcst['total_fcst_hi_only_sans_ubf'],segments_np_ep_ratios['seg_np_open_pipe_amount_' + seg])
        seg_fcst['np_won_and_fcst_hi_sans_ubf'] = list_add(seg_fcst['np_won'], seg_fcst['np_fcst_hi_only_sans_ubf'])
        seg_fcst['np_won_and_fcst_hi'] = list_add(seg_fcst['np_won_and_fcst_hi_sans_ubf'], seg_fcst['ubf'])

        node_fcst['total_fcst_lo_only'] = list_subs(node_fcst['tot_won_and_fcst_lo'], node_fcst['tot_won'])
        seg_fcst['total_fcst_lo_only'] = list_mult(node_fcst['total_fcst_lo_only'],segments_total_open_pipe_ratio['seg_open_pipe_amount_' + seg])
        seg_fcst['total_fcst_lo_only_sans_ubf'] = list_subs(seg_fcst['total_fcst_lo_only'], seg_fcst['ubf'])
        seg_fcst['np_fcst_lo_only_sans_ubf'] = list_mult(seg_fcst['total_fcst_lo_only_sans_ubf'],segments_np_ep_ratios['seg_np_open_pipe_amount_' + seg])
        seg_fcst['np_won_and_fcst_lo_sans_ubf'] = list_add(seg_fcst['np_won'], seg_fcst['np_fcst_lo_only_sans_ubf'])
        seg_fcst['np_won_and_fcst_lo'] = list_add(seg_fcst['np_won_and_fcst_lo_sans_ubf'], seg_fcst['ubf'])

        seg_fcst['ep_fcst_hi_only_sans_won'] = list_mult(seg_fcst['total_fcst_hi_only_sans_ubf'],segments_np_ep_ratios['seg_ep_open_pipe_amount_' + seg])
        seg_fcst['ep_won_and_fcst_hi'] = list_add(seg_fcst['ep_won'], seg_fcst['ep_fcst_hi_only_sans_won'])

        seg_fcst['ep_fcst_lo_only_sans_won'] = list_mult(seg_fcst['total_fcst_lo_only_sans_ubf'],segments_np_ep_ratios['seg_ep_open_pipe_amount_' + seg])
        seg_fcst['ep_won_and_fcst_lo'] = list_add(seg_fcst['ep_won'], seg_fcst['ep_fcst_lo_only_sans_won'])

        seg_fcst['tot_won_and_fcst_hi'] = list_add(seg_fcst['total_fcst_hi_only'], seg_fcst['tot_won'])
        seg_fcst['tot_won_and_fcst_lo'] = list_add(seg_fcst['total_fcst_lo_only'], seg_fcst['tot_won'])

        store['tot_won_and_fcst'] = seg_fcst['tot_won_and_fcst']
        store['ep_won_and_fcst'] = seg_fcst['ep_won_and_fcst']
        store['np_won_and_fcst'] = seg_fcst['np_won_and_fcst']

        store['np_won_and_fcst_hi'] = seg_fcst['np_won_and_fcst_hi']
        store['np_won_and_fcst_lo'] = seg_fcst['np_won_and_fcst_lo']
        store['ep_won_and_fcst_hi'] = seg_fcst['ep_won_and_fcst_hi']
        store['ep_won_and_fcst_lo'] = seg_fcst['ep_won_and_fcst_lo']
        store['tot_won_and_fcst_lo'] = seg_fcst['tot_won_and_fcst_lo']
        store['tot_won_and_fcst_hi'] = seg_fcst['tot_won_and_fcst_hi']

    return store


def get_seg_fcst(report_type, seg, node_fcst, segments_dict, segment_forecast_using_open_pipe, segments_total_open_pipe_ratio=None, segments_open_pipe_ratio=None, segments_np_ep_ratios=None):
    if report_type in ['bookings', 'nextq']:
        seg_fcst = deepcopy(node_fcst)
        node_fcst_ = deepcopy(node_fcst)
        seg_fcst[u'ep_won'] = list_mult(seg_fcst[u'ep_won'], segments_dict['seg_ep_won_'+seg])
        seg_fcst[u'np_won'] = list_mult(seg_fcst[u'np_won'], segments_dict['seg_np_won_' + seg])
        seg_fcst[u'tot_won'] = list_add(seg_fcst[u'ep_won'], seg_fcst[u'np_won'])

        seg_fcst[u'ubf'] = list_mult(seg_fcst[u'ubf'], segments_dict['seg_ubf_fcst_' + seg])
        if not segment_forecast_using_open_pipe:
            # logger.info('inside 2nd if in get_Seg_fcst ')
            seg_fcst[u'ep_won_and_fcst'] = list_mult(seg_fcst[u'ep_won_and_fcst'], segments_dict['seg_ep_fcst_'+seg])
            seg_fcst[u'ep_won_and_fcst_hi'] = list_mult(seg_fcst[u'ep_won_and_fcst_hi'], segments_dict['seg_ep_fcst_'+seg])
            seg_fcst[u'ep_won_and_fcst_lo'] = list_mult(seg_fcst[u'ep_won_and_fcst_lo'], segments_dict['seg_ep_fcst_'+seg])

            pure_np_won_and_fcst = list_subs(seg_fcst[u'np_won_and_fcst'] , node_fcst_[u'ubf'])
            pure_np_won_and_fcst_seg = list_mult(pure_np_won_and_fcst, segments_dict['seg_np_fcst_'+seg])

            seg_fcst[u'np_won_and_fcst'] = list_add(pure_np_won_and_fcst_seg, seg_fcst[u'ubf'])
            ratio_np = list_dev(seg_fcst[u'np_won_and_fcst'], node_fcst[u'np_won_and_fcst'])
            seg_fcst[u'np_won_and_fcst_hi'] = list_mult(seg_fcst[u'np_won_and_fcst_hi'], ratio_np)
            seg_fcst[u'np_won_and_fcst_lo'] = list_mult(seg_fcst[u'np_won_and_fcst_lo'], ratio_np)

            seg_fcst[u'tot_won_and_fcst'] = list_add(seg_fcst[u'ep_won_and_fcst'], seg_fcst[u'np_won_and_fcst'])
            seg_fcst[u'tot_won_and_fcst_hi'] = list_add(seg_fcst[u'ep_won_and_fcst_hi'], seg_fcst[u'np_won_and_fcst_hi'])
            seg_fcst[u'tot_won_and_fcst_lo'] = list_add(seg_fcst[u'ep_won_and_fcst_lo'], seg_fcst[u'np_won_and_fcst_lo'])

        else:
            seg_fcst = get_seg_fcst_based_on_open_pipe(report_type, seg, node_fcst_, seg_fcst,
                                                       segments_total_open_pipe_ratio, segments_open_pipe_ratio,
                                                       segments_np_ep_ratios)

        if report_type == 'nextq':
            seg_fcst[u'ep_epf'] = list_mult(seg_fcst[u'ep_epf'], segments_dict['seg_ep_fcst_'+seg])

    elif report_type == 'expected':
        seg_fcst = deepcopy(node_fcst)
        seg_fcst[u'ep_won_and_fcst'] = list_mult(seg_fcst[u'ep_won_and_fcst'], segments_dict['seg_ep_fcst_'+seg])

        pure_np_won_and_fcst = list_subs(seg_fcst[u'np_won_and_fcst'] , seg_fcst[u'ubf'])
        pure_np_won_and_fcst_seg = list_mult(pure_np_won_and_fcst, segments_dict['seg_np_fcst_'+seg])
        seg_fcst[u'ubf'] = list_mult(seg_fcst[u'ubf'], segments_dict['seg_ubf_fcst_'+seg])
        seg_fcst[u'np_won_and_fcst'] = list_add(pure_np_won_and_fcst_seg, seg_fcst[u'ubf'])

        seg_fcst[u'tot_won_and_fcst'] = list_add(seg_fcst[u'ep_won_and_fcst'], seg_fcst['np_won_and_fcst'])

    elif report_type == 'nextqpipe':
        #TO-DO: maybe we need to change it
        seg_fcst = deepcopy(node_fcst)

    else:
        raise

    return seg_fcst


def gimme_forecast_results(report_type, periods, gbm_config):
    '''
    Given a report type and a set of periods, return the combined results by period

    Arguments:
        report_type {str} - The report type name (bookings, bookings_expected, etc)
        periods {list of str} - A list of periods to get the results for
    Returns:
        A dict of combined results by period
    '''
    output = {}
    for period in periods:
        qperiod, mperiod, _ = janky_period_parser(period)
        params = create_cr_params(report_type, qperiod, mperiod, gbm_config)
        raw_results = retrieve_combined_results_helper(params, params['target'], params['target_key'],
                                                       gbm_config=gbm_config)
        output[period] = {k1: v1 for k0, v0 in raw_results.items() for k1, v1 in v0.items()}

        ds = get_dataset('OppDS', gbm_config)

        bands_config = ds.models['common'].config.get('bands_config', {})
        if bands_config.get('force_reduce_bands'):
            reduction_factor = bands_config.get('reduction_factor', 0.99)
            band_range = bands_config.get('hi_low_band_range_wrt_fcst', None) #[0.9, 1.1]
            hi_fields = ['ep_won_and_fcst_hi',
                         'np_won_and_fcst_hi',
                         'tot_won_and_fcst_hi']
            lo_fields = ['ep_won_and_fcst_lo',
                         'np_won_and_fcst_lo',
                         'tot_won_and_fcst_lo']
            day = 60 * 60 * 24 * 1000
            for node in output[period]:
                beg = output[period][node]['as_of'][0]
                for i in range(0,len(output[period][node]['as_of'])):
                    curr_day = int(output[period][node]['as_of'][i] - beg)/day
                    for fld in hi_fields:
                        if fld in output[period][node]:
                            if band_range and output[period][node][fld[:-3]][i] != 0:
                                perc = abs(output[period][node][fld][i] - output[period][node][fld[:-3]][i]) / \
                                       output[period][node][fld[:-3]][i]
                                if perc > (band_range[1] - 1):
                                    output[period][node][fld][i] = output[period][node][fld[:-3]][i] * band_range[1]

                            diff = output[period][node][fld][i] - output[period][node][fld[:-3]][i]
                            output[period][node][fld][i] -= ((1 - (reduction_factor ** curr_day)) * diff)

                    for fld in lo_fields:
                        if fld in output[period][node]:
                            if band_range and output[period][node][fld[:-3]][i] != 0:
                                perc = abs(output[period][node][fld][i] - output[period][node][fld[:-3]][i]) / \
                                       output[period][node][fld[:-3]][i]
                                if perc > (1 - band_range[0]):
                                    output[period][node][fld][i] = output[period][node][fld[:-3]][i] * band_range[0]

                            diff = output[period][node][fld[:-3]][i] - output[period][node][fld][i]
                            output[period][node][fld][i] += ((1 - (reduction_factor ** curr_day)) * diff)

        er_mult = ds.models['common'].config.get('er_config', {}).get('mult')
        if report_type == 'expected' and er_mult:
            for node in output[period]:
                try:
                    if output[period][node]['ep_won_and_fcst'][-1] < output[period][node]['ep_won_and_fcst'][-2]:
                        ratio = (output[period][node]['ep_won_and_fcst'][-1] * er_mult)/output[period][node]['ep_won_and_fcst'][-2]
                        for i in range(0, len(output[period][node]['ep_won_and_fcst'])-1):
                            output[period][node]['ep_won_and_fcst'][i] *= ratio
                            output[period][node]['tot_won_and_fcst'][i] = output[period][node]['ep_won_and_fcst'][i] + output[period][node]['np_won_and_fcst'][i]
                except:
                    pass

        # check and if segment forecast enabled then convert node values into segments format
        ds = get_dataset('OppDS', gbm_config)
        segment_config = ds.models['common'].config.get('segment_config', {})
        #For elastic
        segment_forecast_using_open_pipe = segment_config.get('segment_forecast_using_ACV_open_pipe', False)
        segment_forecast = segment_config.get('show_forecast', False)
        segment_amount = segment_config.get('amounts', {})
        terminal_fate_fld = segment_config.get('terminal_fate_fld', 'terminal_fate')
        logger.info('segment_forecast: '+str(segment_forecast))
        logger.info('segment_amount: '+str(segment_amount))
        logger.info('segmrnt terminal fate: '+str(terminal_fate_fld))

        # ns_config = ds.models['common'].config.get('viewgen_config', {})['node_seg_config']
        ns_config = ds.models['common'].config.get('viewgen_config', {}).get('node_seg_config')
        seg_forecasts = ['seg_ep_fcst', 'seg_ep_won', 'seg_np_fcst', 'seg_np_won', 'seg_ubf_fcst']

        if segment_forecast:
            churn_segment = ds.models['common'].config.get('segment_config', {}).get('churn_segment')
            if report_type in ['bookings', 'expected', 'nextq']:
                for node in output[period]:
                    res = {}
                    node_fcst = deepcopy(output[period][node])
                    if segment_forecast_using_open_pipe:
                        seg_flds = [i for i in node_fcst.keys() if i.startswith('seg')]
                        segment_open_pipe_vals = {fld: node_fcst.pop(fld) for fld in seg_flds if
                                                  fld.startswith('seg') and 'open_pipe_amount' in fld}
                        segment_total_open_pipe_vals = {}
                        for seg in segment_amount:
                            segment_total_open_pipe_vals['seg_open_pipe_amount_'+seg] = list_add(segment_open_pipe_vals['seg_ep_open_pipe_amount_'+seg], segment_open_pipe_vals['seg_np_open_pipe_amount_'+seg])

                    extra_flds = [i for i in node_fcst.keys() if
                                  i.startswith('seg') and ('amount' in i or 'open_pipe' in i)]
                    for k in extra_flds:
                        node_fcst.pop(k, None)
                    seg_flds = [i for i in node_fcst.keys() if i.startswith('seg')]
                    segments_ratio = {fld: node_fcst.pop(fld) for fld in seg_flds}
                    segments_ratio = calibrate_segment_ratios(seg_forecasts, node, segments_ratio, ns_config, churn_segment)

                    segments_total_open_pipe_ratio = None
                    segments_open_pipe_ratio = None
                    segments_np_ep_ratios = None
                    if segment_forecast_using_open_pipe:
                        seg_opens = ['seg_ep_open_pipe_amount', 'seg_np_open_pipe_amount']

                        segment_open_pipe_vals_copy = deepcopy(segment_open_pipe_vals)
                        segment_total_open_pipe_vals_copy = deepcopy(segment_total_open_pipe_vals)

                        segments_open_pipe_ratio = calibrate_segment_ratios(seg_opens, node, segment_open_pipe_vals, ns_config, churn_segment)

                        segments_total_open_pipe_ratio = calibrate_segment_ratios(['seg_open_pipe_amount_'], node, segment_total_open_pipe_vals,
                                                                            ns_config, churn_segment)

                        segments_np_ep_ratios = new_calibrate(node, segment_open_pipe_vals_copy ,
                                                              segment_total_open_pipe_vals_copy, ns_config)
                    res[u'all_deals'] = deepcopy(node_fcst)
                    for amt in segment_amount:
                        res[amt] = get_seg_fcst(report_type, amt, node_fcst, segments_ratio, segment_forecast_using_open_pipe, segments_total_open_pipe_ratio, segments_open_pipe_ratio, segments_np_ep_ratios)

                    output[period][node] = res.copy()
    return output


def create_cr_params(report_type, qperiod, mperiod, gbm_config):
    amounts = ['tot_won_and_fcst', 'tot_won_and_fcst_lo', 'tot_won_and_fcst_hi', 'tot_won',
               'ep_won_and_fcst', 'ep_won_and_fcst_lo', 'ep_won_and_fcst_hi', 'ep_won',
               'np_won_and_fcst', 'np_won_and_fcst_lo', 'np_won_and_fcst_hi', 'np_won',
               'ubf'
               ]

    atds = get_dataset('AvisoTaskDS', gbm_config)
    bs_enabled = atds.params['general'].get('globals', {}).get('run_blackswan', False)
    if bs_enabled:
        amounts += ['bs_tot_won_and_fcst', 'bs_tot_won_and_fcst_lo', 'bs_tot_won_and_fcst_hi',
                    'bs_ep_won_and_fcst', 'bs_ep_won_and_fcst_lo', 'bs_ep_won_and_fcst_hi',
                    'bs_np_won_and_fcst', 'bs_np_won_and_fcst_lo', 'bs_np_won_and_fcst_hi',
                    'bs_ubf'
                    ]


    #when segment forecast is enabled, add segment fields
    ds = get_dataset('OppDS', gbm_config)
    bs_enabled = ds.params['general'].get('globals', {}).get('run_blackswan', False)
    segment_config = ds.models['common'].config.get('segment_config', {})
    segment_forecast_enabled = segment_config.get('show_forecast', False)
    segment_forecast_using_open_pipe = segment_config.get('segment_forecast_using_ACV_open_pipe', False)
    if segment_forecast_enabled:
        segment_amount = segment_config.get('amounts', {})
        # a segment field looks like "seg_ep_fcst__NRR ACV"
        segment_flds = [(fld+"_"+seg_amt_key) for fld in ['seg_ep_fcst', 'seg_ep_won', 'seg_np_fcst', 'seg_np_won', 'seg_ubf_fcst', 'seg_ep_fcst_amount',
                                                          'seg_ep_won_amount', 'seg_np_fcst_amount',
                                                          'seg_np_won_amount', 'seg_ubf_fcst_amount'] for seg_amt_key in segment_amount.keys()]

        if segment_forecast_using_open_pipe:
            segment_flds += [(fld + "_" + seg_amt_key) for fld in
                            ['seg_ep_open_pipe', 'seg_np_open_pipe','seg_ep_open_pipe_amount', 'seg_np_open_pipe_amount' ] for seg_amt_key
                            in segment_amount.keys()]

        amounts += segment_flds

    #first setup the basic dicts
    if report_type == 'bookings':
        output = {
            'target': 'bookings',
            'target_key': 'main',
            'report_spec': 'detailed_bookings',
            'fields': amounts + ['as_of', 'quarter', 'cache_key', 'horizon'],
            }
    elif report_type == 'expected':
        exp_flds = ['tot_won_and_fcst', 'ep_won_and_fcst', 'np_won_and_fcst', 'as_of', 'quarter', 'horizon']
        output = {
            'target': 'bookings',
            'target_key': 'expected',
            'report_spec': 'detailed_bookings',
            #'begin_month_mnemonic': 1,
            #'as_of_month_mnemonic': 1,
            'fields':  exp_flds + ['ubf'] + segment_flds if segment_forecast_enabled else exp_flds,
            }
    elif report_type == 'nextq':
        output = {
            'target': 'fwd_bookings',
            'target_key': 'main',
            'report_spec': 'detailed_fwd_bookings',
            'fields': amounts + ['as_of', 'quarter', 'cache_key', 'horizon', 'ep_epf'],
            }
    elif report_type == 'nextqpipe':
        output = {
            'target': 'nextqpipe',
            'target_key': 'main',
            'report_spec': 'detailed_nextqpipe',
            'fields': ['unborn_existing', 'unborn_total', 'as_of', 'quarter', 'cache_key', 'horizon', 'ep_epf'],
            }
    else:
        raise

    # set up groupbys and mnemonics
    output['groupby'] = TargetSpec.getFullByName(output['target'], gbm_config).drilldowns
    output['mnemonic'] = qperiod

    is_quarterly = gbm_config.get_config(category='forecast', config_name='tenant').get('forecast_period', 'Q') == 'Q'

    # if monthly isn't a thing, the target/target_key/report_spec are good enough to describe the results
    if is_quarterly:
        return output

    # otherwise we need to add a bunch of optional shit to clarify which results we want
    # if we stored results in a less insane way, we wouldn't have to do this
    # TODO: if dell ever wants monthly we're fucked

    # if we're monthly but looking for quarter CR -> modify the dict so it filters down correctly
    if not mperiod:
        if report_type == 'expected':
            output.update({'begin_month_mnemonic': 1, 'as_of_month_mnemonic': 1})
        elif report_type == 'bookings':
            output.update({'begin_month_mnemonic': 1, 'horizon_month_mnemonic': 3})
    # otherwise, we calculate the month index based off some period jankery
    else:
        q_dtls = period_details_by_mnemonic(qperiod)
        m_dtls = period_details_by_mnemonic(mperiod, period_type='CM')
        # they aren't exact so closeish
        delta = datetime.timedelta(seconds=1)
        if m_dtls.begin - delta < q_dtls.begin < m_dtls.begin + delta:
            month_idx = 1
        elif m_dtls.end - delta < q_dtls.end < m_dtls.end + delta:
            month_idx = 3
        else:
            month_idx = 2
        output.update({
            'begin_month_mnemonic': month_idx,
            'as_of_month_mnemonic': month_idx,
            'horizon_month_mnemonic': month_idx,
            })
    return output
