"""The sandbox shell verbs, ported from service-infrastructure/aviso/sdk/sandbox.py.

Registered in shell.py as ``sandbox``. This runs on the client, inside the
package preparesdk builds, so the paths are written against the ``gbm/`` mount
that aviso_core/urls.py gives this app.

``list``, ``list_available``, ``get_diff`` and ``commit`` are ported. The legacy
class also had purge, resync, clear_data and list_stale, whose endpoints
(/sandbox/purge, /sandbox/resync, ...) are not served here -- the same reason
data.py leaves out ``list`` and ``indexes``.

get_diff needs two things beyond /sandbox/list: ``shell.tenant('get_all_config')``
for the tenant config on both sides of the comparison, and ``shell.dataset`` for
each dataset. Both are answered now, so commit prints the same diff the legacy
shell did before asking for confirmation.
"""
from __future__ import print_function

import logging
from pprint import pprint

import six

# SDK clients still run Python 2, so this has to import under both. See the
# same block in ../interface/base.py.
basestring = six.string_types[0]

from ..interface.base import Argument, BaseSDKFunction, CommandMeta

logger = logging.getLogger()

BASE_PATH = '/gbm/sandbox'


class SandboxPythonSdkFunctions(BaseSDKFunction):

    default_command = 'commit'

    meta_commands = {
        'list': CommandMeta(
            help='Fetch list of sandboxes',
            name='sandbox list',
            url_pattern=None,
            validations=None,
            arguments=[]
        ),
        'list_available': CommandMeta(
            help='list all sandboxes',
            name='sandbox',
            url_pattern=None,
            validations=None,
            arguments=[]
        ),
        'get_diff': CommandMeta(
            help='get the changes in specified sandbox',
            name='sandbox',
            url_pattern=None,
            validations=None,
            arguments=[
                Argument(name='sandbox', arg_type=(basestring,), required=True,
                         help='sandbox name')
            ]
        ),
        'commit': CommandMeta(
            help='Commit a sandbox',
            name='sandbox commit',
            url_pattern=None,
            validations=None,
            arguments=[
                Argument(name='sandbox', arg_type=(basestring,),
                         help='This argument is to get sandbox name'),
                Argument(name='confirm', arg_type=(bool,), required=False,
                         help='confirmation'),
                Argument(name='drop_sandbox_data', arg_type=(bool,), required=True,
                         help='drops sandbox data if True', default=False),
                Argument(name='is_self_serve_call', arg_type=(bool,), required=False,
                         help='Indicates if the commit is triggered from self serve module')
            ]
        ),
    }

    def list(self, shell):
        return shell.api('%s/list' % BASE_PATH, None)

    def list_available(self, shell):
        sandboxes = shell.api('%s/list' % BASE_PATH, None)
        return list(list(sandbox.keys())[0] for sandbox in sandboxes)

    def check_sandbox(self, shell, sandbox):
        if sandbox is None:
            raise Exception('Required sandbox name')
        if sandbox not in self.list_available(shell):
            raise Exception("Required sandbox %s is not available " % sandbox)
        return True

    def get_diff(self, shell, sandbox=None):
        # get the current config, sandboxed_config to show the diff to user
        if not sandbox:
            sandbox = getattr(shell, 'entered_sandbox', None)
            if not sandbox:
                raise Exception("Required argument sandbox is not found!!")
        self.check_sandbox(shell, sandbox)
        current_config = shell.tenant('get_all_config')
        sandboxed_config = shell.tenant('get_all_config', sandbox=sandbox)

        current_datasets = current_config.pop('datasets', {})
        sandboxed_datasets = sandboxed_config.pop('datasets', {})
        total_diff = shell.diff_rec(current_config, sandboxed_config)
        dataset_list = set(current_datasets.keys()) | set(sandboxed_datasets.keys())

        diff_available = False
        if total_diff:
            diff_available = True
            pprint(total_diff)

        for dataset_name in dataset_list:
            ds_config = shell.dataset(dataset=dataset_name) or {}
            ds_sandbox_config = shell.dataset(dataset=dataset_name, sandbox=sandbox) or {}
            diff = shell.diff_rec(ds_config, ds_sandbox_config)
            if diff:
                diff_available = True
                print("diff in %s are " % dataset_name)
                pprint(diff)
        return diff_available

    def commit(self, shell, sandbox=None, confirm=False,
               drop_sandbox_data=False, is_self_serve_call=False):
        # 'sandbox' is not one of BaseSDKFunction.valid_shell_defaults, so an
        # entered sandbox is not filled in for us the way dataset/stage are.
        # Resolved before the diff rather than after, so the name that gets
        # committed is the one that was diffed -- legacy read shell.entered_sandbox
        # inside get_diff and then posted the unresolved argument.
        if not sandbox:
            sandbox = getattr(shell, 'entered_sandbox', None)
        if not sandbox:
            raise Exception("Required argument sandbox is not found!!")

        if not confirm:
            diff_available = self.get_diff(shell, sandbox=sandbox)
            if not diff_available:
                print("Looks like there is no changes in the sandbox")
                return

        post_data = {'sandbox_name': sandbox}
        if is_self_serve_call:
            post_data['is_self_serve_call'] = True
        if drop_sandbox_data:
            post_data['clear_sandbox_data'] = True
        kwargs = {'confirm': confirm} if confirm else {}
        if shell.confirm("Are you sure to commit changes", kwargs):
            return shell.api('%s/commit' % BASE_PATH, post_data)
