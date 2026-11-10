"""GBM-side configs for ``/gbm/forecast_results``: target specs and datasets.

Legacy gbm-service reads ``target_spec/<name>``, ``datasets/<name>`` and ``forecast/tenant`` through
``sec_context.details``, which in gbm-service is the GBM microservice Postgres. In aviso-core
``sec_context.details`` is the FM Postgres, so these are read through a ``GbmTenantConfigReader``
(``utils/db_read_utils.py``) instead: the same ``tenant_configs`` / ``tenant_flags`` rows legacy reads.

Ported from service-infrastructure:

* ``config_pattern_expansion``: ``aviso/utils/configUtils.py``, verbatim except that ``tdetails`` (the
  object ``$(category/name)`` references are resolved against) is passed in instead of being
  ``sec_context.details``.
* ``TargetSpec``: ``aviso/domainmodel/datameta.py`` (the microservice version, which reads tenant config,
  not the older Mongo-collection ``TargetSpec`` in ``gbm_apis/domainmodel/datameta.py``). ``save`` (a
  write) and the unused ``validate`` are not ported.
* ``get_dataset``: ``DSClass.getByName`` + ``DSClass.getByNameAndStage`` for the ``sandbox=None`` path,
  which is the only one this endpoint uses.

Read-only: nothing here writes anywhere.
"""
import copy
import logging

from aviso.utils import GnanaError

from gbm_apis.deal_result.dataset import Dataset

logger = logging.getLogger('gnana.%s' % __name__)


def config_pattern_expansion(attrs, tdetails):
    eval_errs = {}

    def get_keys(attrs, item, path, mainpath):
        if isinstance(item, dict):
            mainpath = path
            for key in item.keys():
                path = mainpath + "['" + str(key) + "']"
                get_keys(attrs, item[key], path, mainpath)
        elif hasattr(item, '__iter__'):
            mainpath = path
            for i in item:
                if isinstance(i, dict):
                    get_keys(attrs, i, path, mainpath)
                else:
                    expr = copy.copy(item)
                    err = checkpattern(attrs, i, path, "list", in_and_ex=item)
                    if err:
                        eval_errs[path[path.find('[') + 2:-2]] = [expr, err]
        elif not hasattr(item, '__iter__'):
            err = checkpattern(attrs, item, path, "dict", in_and_ex=None)
            if err:
                eval_errs[path[path.find('[') + 2:-2]] = [item, err]
#                 eval_errs[path] = checkpattern(attrs, item, path, "dict", in_and_ex=None)

    def checkpattern(attrs, i, path, i_type, in_and_ex):
        if isinstance(i, str):
            if ((i.startswith("$(") or i.startswith("${") or i.startswith("$[")) and i.find('/') >= 2):
                pattern_type = i[1:2]
                category_key, config_name = i[2:-1].split("/")
                pattern_value = None
                pattern_value = copy.copy(tdetails.get_config(category_key, config_name, None))
                if not pattern_value:
                    return ["Invalid expression in configuration. Value cannot be derived from Tenant config."]
                if isinstance(pattern_value, dict) and pattern_type != "{":
                    return "Pattern mismatch in configuration.Pattern is %s" % i
                elif isinstance(pattern_value, list) and pattern_type != "[":
                    return "Pattern mismatch in configuration.Pattern is %s" % i
                last_item = path.rfind("[")
                if i_type == 'dict':
                    last_item = path.rfind("[")
                    eval(path[:last_item])[path[last_item + 2:-2]] = pattern_value
                elif i_type == 'list':
                    in_and_ex_err = []
                    if len(in_and_ex) > 3 and (isinstance(in_and_ex, list) or isinstance(in_and_ex, tuple)):
                        in_and_ex_err.append("Incorrect expression. Expression must be in format 'expression, " +
                                             "inclusion list or map, exclusion list'")
                    if isinstance(pattern_value, list):
                        if len(in_and_ex) == 3:
                            for every in in_and_ex[2]:
                                if every in pattern_value:
                                    pattern_value.remove(every)
                                else:
                                    in_and_ex_err.append(" %s is not present in Tenant config" % every)
                        for every_value in in_and_ex[1]:
                            if every_value in pattern_value:
                                in_and_ex_err.append("%s should be in exclusion list as you are trying" % every_value +
                                                     " to modify the overridden Tenant config")
                            pattern_value.append(every_value)
                    if isinstance(pattern_value, dict):
                        if len(in_and_ex) == 3:
                            for every in in_and_ex[2]:
                                if every in pattern_value.keys():
                                    pattern_value.pop(every)
                                else:
                                    in_and_ex_err.append(" %s is not present in Tenant config" % every)
                        for every_key in in_and_ex[1].keys():
                            if every_key in pattern_value.keys():
                                in_and_ex_err.append(" %s should be in exclusion list as you are " % every_key +
                                                     "trying to modify the overridden Tenant config")
                            pattern_value[every_key] = in_and_ex[1][every_key]
                    if in_and_ex_err:
                        return in_and_ex_err
                    del(eval(path)[1:])
                    eval(path[:last_item])[path[last_item + 2:-2]] = pattern_value
        return None
    get_keys(attrs, attrs, 'attrs', '')
    return {'attrs': attrs, 'eval_errs': eval_errs}


class TargetSpec(object):
    """New TargetSpec class to support microservices architecture"""

    def __init__(self, attrs=None):
        self.name = None
        self.models = {}
        self.drilldowns = []
        self.report_spec = {}
        self.keys = {}
        self.module = ""
        self.task_v2 = False
        if attrs:
            self.decode(attrs)

    def encode(self, attrs):
        attrs['name'] = self.name
        attrs['models'] = self.models
        attrs['drilldowns'] = self.drilldowns
        attrs['module'] = self.module
        attrs['keys'] = self.keys
        attrs['report_spec'] = self.report_spec
        attrs['task_v2'] = self.task_v2
        return

    def decode(self, attrs):
        self.name = attrs['name']
        self.models = attrs['models']
        self.drilldowns = attrs['drilldowns']
        self.module = attrs['module']
        self.keys = attrs['keys']
        self.task_v2 = attrs.get('task_v2', False)
        if 'report_spec' in attrs:
            self.report_spec = attrs['report_spec']
        return

    def get_combine_options(self, target_key, dataset, modelname):
        if target_key in self.keys:
            for d, m, combine_options in self.keys.values():
                if dataset == d and modelname == m:
                    return combine_options
        for d, m, combine_options in self.models.values():
            if dataset == d and modelname == m:
                return combine_options
        return {}

    @classmethod
    def getFullByName(cls, name, gbm_config):
        ten = gbm_config
        attrs = ten.get_config('target_spec', name)
        if not attrs:
            return None
        res = config_pattern_expansion(attrs, gbm_config)
        target_spec_attrs = res.get('attrs', None)
        eval_errs = res.get('eval_errs', None)
        if eval_errs:
            tdetails = gbm_config
            for key in eval_errs.keys():
                if key in tdetails.get_flag('save_on_error', name, {}):
                    eval_errs.pop(key)
            if eval_errs:
                raise GnanaError(eval_errs)
        return cls(target_spec_attrs)


def get_dataset(name, gbm_config):
    """Legacy ``Dataset.getByNameAndStage(name)`` with ``sandbox=None``, reading from the GBM Postgres.

    The body is legacy ``DSClass.getByName`` followed by ``DSClass.getByNameAndStage``, kept line for line,
    including two legacy behaviours: any error while loading is logged and turned into ``None``, and a
    pattern-expansion error raises ``TypeError`` (``dataset.name + '~' + sandbox`` with ``sandbox=None``).
    """
    sandbox = None  # this endpoint never passes a sandbox; kept so the ported lines below read as legacy

    # --- DSClass.getByName(name, sandbox=None)
    try:
        attrs = gbm_config.get_config('datasets', name)
        if not attrs:
            return None
        dataset = Dataset(attrs)
    except Exception as e:
        logger.exception(e)
        return None

    # --- DSClass.getByNameAndStage(name, sandbox=None, target='_data', full_config=True)
    dataset.stage_used = sandbox
    # sandbox is None, so legacy's inbox-entry branch is never taken
    dataset.InboxEntryClass = None
    dataset.InboxFileEntryClass = None
    attrs = {}
    dataset.encode(attrs)
    res = config_pattern_expansion(attrs, gbm_config)
    dataset_attrs = res.get('attrs', None)
    eval_errs = res.get('eval_errs', None)
    tdetails = gbm_config
    if eval_errs:
        for key in eval_errs.keys():
            if (key in tdetails.get_flag('save_on_error', dataset.name + '~' + sandbox, {}) or
                    key in tdetails.get_flag('save_on_error', dataset.name, {})):
                eval_errs.pop(key)
        if eval_errs:
            raise GnanaError(eval_errs)
    dataset.decode(dataset_attrs)
    return dataset
