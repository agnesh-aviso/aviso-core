"""Authorization token registration, ported from service-infrastructure/aviso/api/auth_tokens.py.

A calling service (the forecast app, say) identifies itself to this one with a
per-tenant token sent as the ``Access-Token`` header. That only works once the
token has been registered here, which tenant onboarding does in two calls:

- ``GetAuthorizationTokenDetails`` -- this tenant's own token, for registering
  with the services this one calls (ETL).
- ``SetAuthenticationToken`` -- register a caller's token for the tenant the
  request is switched to.

Legacy ran one deployment per stack, so each wrote to its own
authorization_tokens table. This service serves several stacks and the table is
kept per stack, so both views work in the stack of the tenant the request is
switched to -- the same place accounts.context and baseView read tokens from.

Legacy get_all_tokens and remove_auth_token are not ported.
"""
import json
import logging

from django.http import HttpResponseNotFound
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from psycopg2 import ProgrammingError

from aviso.domainmodel.auth_tokens import AuthorizationToken
from aviso.framework import TenantStackPostgresContext
from aviso.framework.connection_factory import ConnectionFactory
from aviso.settings import STACK_DB_TYPE, sec_context
from aviso.utils import GnanaError, is_true

from gbm_apis.framework.baseView import AvisoView
from gbm_apis.framework.mixins import AvisoCompatibilityMixin

logger = logging.getLogger('gnana.%s' % __name__)


@method_decorator(csrf_exempt, name='dispatch')
class SetAuthenticationToken(AvisoCompatibilityMixin, AvisoView):
    """Register a calling service's token for the current tenant.

    PostData:

            tenant : the tenant's name in the calling service
            stack : the calling service's stack
            auth_token : the token that service will send as Access-Token
            primary : whether that service is the tenant's primary registrant

    The row is saved in the current tenant's stack database, with the current
    tenant as its source_tenant. A tenant has one primary token at a time.
    """

    http_method_names = ['post']

    restrict_to_roles = {AvisoView.Role.Administrator, AvisoView.Role.Gnacker}

    def post(self, request, *args, **kwargs):
        post_data = json.loads(request.body)
        required_params = {'tenant', 'stack', 'auth_token'}
        for req_param in required_params:
            if not post_data.get(req_param):
                return HttpResponseNotFound(f"{req_param} is not found in post data")
        primary = post_data.get('primary')
        source_tenant = sec_context.details.name
        primary_check_criteria = {"object.primary_tenant": primary,
                                  'object.source_tenant': sec_context.name}
        # One block for the check, the lookup and the save, so all three run
        # against the same stack database.
        with TenantStackPostgresContext(sec_context.name):
            if primary and AuthorizationToken.get_count(primary_check_criteria):
                # "sucess" is legacy's spelling, kept for callers that read it.
                return {"sucess": False, "success": False,
                        "reason": "primary tenant is already there"}
            try:
                authorization_token = AuthorizationToken.getByFieldValue(
                    "token", post_data['auth_token'])
            except Exception:
                authorization_token = None
            if not authorization_token:
                authorization_token = AuthorizationToken()
            authorization_token.token = post_data['auth_token']
            authorization_token.tenant = post_data['tenant']
            authorization_token.stack = post_data['stack']
            authorization_token.primary_tenant = is_true(post_data.get('primary', False))
            authorization_token.source_tenant = source_tenant
            try:
                authorization_token.save()
            except ProgrammingError as pe:
                raise GnanaError(str(pe))

        return {'success': True}


class GetAuthorizationTokenDetails(AvisoCompatibilityMixin, AvisoView):
    """The current tenant's own token, as the three fields set_auth_token takes.

    ``stack`` is the stack the tenant is on. Legacy answered with its CNAME,
    which was the same thing when a deployment served one stack.
    """

    http_method_names = ['get']

    restrict_to_roles = {AvisoView.Role.Administrator, AvisoView.Role.Gnacker,
                         AvisoView.Role.User}

    def get(self, request, *args, **kwargs):
        current_tenant = sec_context.details
        if not current_tenant.auth_token:
            return HttpResponseNotFound("Token not found...")
        return {'tenant': current_tenant.name,
                'auth_token': current_tenant.auth_token,
                'stack': ConnectionFactory.ms_stack(current_tenant.name, STACK_DB_TYPE)}
