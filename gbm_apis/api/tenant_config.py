"""The tenant config dump, ported from service-infrastructure/aviso/api/tenantmanager.py.

Two views are served:

- ``TenantConfigDump`` -- the whole config. The SDK reaches it as
  ``shell.tenant('get_all_config')``, which is what sandbox get_diff compares
  a sandbox against.
- ``TenantConfig`` -- one category/config_name entry, read and written. The SDK
  reaches it as ``shell.tenant('get_config', ...)`` and
  ``shell.tenant('set_config', ...)``; current_period() depends on the read to
  find forecast/quarter_begins.

The rest of legacy tenantmanager -- user management, flag writes, bulk_config,
validate_config -- is not ported.
"""
import json
import logging

from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from aviso.domainmodel.app import User
from aviso.domainmodel.sandbox import Sandbox, recache_dataset_config
from aviso.framework import LoginUserContext
from aviso.settings import sec_context
from aviso.utils import GnanaError, dateUtils, is_prod, is_true
from aviso.utils.stringUtils import html_escape

from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin

logger = logging.getLogger('gnana.%s' % __name__)


class TenantConfigDump(AvisoCompatibilityMixin, AvisoView):
    """Dump the whole tenant configuration as a JSON object.

    Reads the sandboxed config when ?sandbox= is given, which is the only
    reason get_diff can show a before and after: the same endpoint answers for
    both sides of the comparison.

    Legacy also folded a maintenance_message into the downtime flag through
    get_maint_message(); that helper is not in this service's aviso package, so
    a ?flag=downtime request returns the stored flag without it.
    """

    http_method_names = ['get']

    restrict_to_roles = AvisoView.Role.All_Roles

    def get(self, request, tenant_name=None, *args, **kwargs):
        tenant_name = tenant_name.lower()
        sandbox = self.request_sandbox
        if sec_context.name != tenant_name:
            return HttpResponseForbidden(
                "Unauthorized to change the configuration")
        tdetails = sec_context.details
        cfg = tdetails.get_all_config(sandbox=sandbox)
        cfg['timezoneoffset'] = dateUtils.get_tenant_timezone_offset()
        config = cfg
        ret_flags = request.GET.getlist('flag', [])
        flags = {}
        for flag in ret_flags:
            flags.update({html_escape(flag) if isinstance(flag, str) else flag:
                          tdetails.get_all_flags().get(flag, {})})
        if ret_flags and flags:
            config = {'config': cfg, 'flags': flags}

        return config


@method_decorator(csrf_exempt, name='dispatch')
class TenantConfig(AvisoCompatibilityMixin, AvisoView):
    """Read or change one tenant config entry.

    GET returns the value stored for the category and config_name, from the
    sandbox when ?sandbox= is given.

    POST saves the posted JSON as the new value and returns the old one. It
    always writes into a sandbox -- the change reaches the live config only
    when that sandbox is committed, or straight away with ?auto_commit=true.
    ?action=remove and ?action=remove_path delete instead of saving.
    """

    http_method_names = ['get', 'post']

    restrict_to_roles = AvisoView.Role.All_Roles

    def get(self, request, tenant_name=None,
            category=None, config_name=None, *args, **kwargs):
        tenant_name = tenant_name.lower()
        sandbox = self.request_sandbox
        if sec_context.name != tenant_name:
            return HttpResponseForbidden(
                "Unauthorized to change the configuration")

        if request.GET.get('action'):
            return HttpResponseForbidden(
                "You can't specify action in GET.  Perhaps you want to POST??")
        conf_details = sec_context.details.get_config(
            category, config_name, sandbox=sandbox)
        if conf_details is None:
            return HttpResponseNotFound(
                "No config item: %s found for category: %s " % (
                    config_name, category))
        return conf_details

    def post(self, request, tenant_name=None,
             category=None, config_name=None, *args, **kwargs):
        with LoginUserContext(request.user.username):
            requesting_user = User.getUserByLogin(request.user.username)
            if (AvisoView.Role.Administrator not in requesting_user.roles and
                    AvisoView.Role.Gnacker not in requesting_user.roles):
                return HttpResponseForbidden(
                    "Unauthorized to change the configuration")
        tenant_name = tenant_name.lower()
        if sec_context.name != tenant_name:
            return HttpResponseForbidden(
                "Unauthorized to change the configuration")
        tdetails = sec_context.details
        old_config = tdetails.get_config(category, config_name, "")
        action = request.GET.get('action', 'save')
        comment = request.GET.get('comment')
        sandbox_name = self.request_sandbox
        if sandbox_name == '_data':
            raise GnanaError({'Error': '_data is reserved for sandbox name. Use another name instead!'})
        auto_commit = is_true(request.GET.get('auto_commit'))
        if not sandbox_name:
            raise GnanaError({'Error': 'Please provide a sandbox name to set tenant config!'})
        if not comment:
            if is_prod():
                raise GnanaError({'Error': 'Comment is Mandatory! Please use tenant set_config command with comment'})
            comment = 'Modified ' + tenant_name + ' Config'

        if action == 'save':
            new_config = json.loads(request.read())
            if category == 'ui':
                # ui entries live under forecast/tenant's ui_config rather
                # than in a category of their own.
                tenant_config_details = tdetails.get_config('forecast', 'tenant', {})
                if 'ui_config' not in tenant_config_details:
                    tenant_config_details['ui_config'] = {}
                old_config = tenant_config_details['ui_config'].get(config_name)
                tenant_config_details['ui_config'][config_name] = new_config
                tdetails.set_config('forecast', 'tenant',
                                    tenant_config_details,
                                    sandbox_name=sandbox_name,
                                    sandbox_comment=comment)
            else:
                tdetails.set_config(category, config_name, new_config,
                                    sandbox_name=sandbox_name,
                                    sandbox_comment=comment)
        elif action == 'remove':
            try:
                tdetails.remove_config(category, config_name,
                                       sandbox_name=sandbox_name,
                                       sandbox_comment=comment)
            except KeyError:
                return HttpResponseNotFound(
                    "No config item: %s found for category: %s " % (
                        config_name, category))
        elif action == 'remove_path':
            try:
                tdetails.remove_path_in_config(category, config_name,
                                               sandbox_name=sandbox_name,
                                               sandbox_comment=comment)
            except Exception as e:
                logger.exception(e)
                return HttpResponseNotFound(str(e))
        else:
            raise GnanaError("Unknown action")

        if auto_commit:
            try:
                logger.info("Committing sandbox %s", sandbox_name)
                Sandbox.commit(sandbox_name)
            except Exception as e:
                logger.exception(e)
                return {'success': False,
                        'message': str(e)}
        # Legacy also wrote an access_logs entry here (events tcu / tcr). That
        # helper needs the accesslogs Django app, which this service does not
        # install, so the change is recorded in the service log only.
        if action in ('save', 'remove'):
            logger.info("%s %s %s for category %s in sandbox %s",
                        request.user.username,
                        'updated' if action == 'save' else 'removed',
                        config_name, category, sandbox_name)
        recache_dataset_config(sandbox=sandbox_name)
        return old_config
