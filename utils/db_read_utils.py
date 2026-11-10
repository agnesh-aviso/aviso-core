import psycopg2
import psycopg2.extras
import json
import logging

from aviso.framework.connection_factory import ConnectionFactory
from aviso.utils import GnanaError

logger = logging.getLogger('gnana.%s' % __name__)

def get_oppds_config(tenant_name, service):
    # note: since we are using pgbouncer at the front this should be fine.
    # TODO: find a way to use a connection pool and db access should be handled via a context manager.
    supported_services = ['gbm', 'etl']
    if service not in supported_services:
        raise ValueError(f"service must be one of {supported_services}")
    stack = ConnectionFactory._get_ms_stack(tenant_name, service)
    conn = ConnectionFactory._get_pg_conn(db_type=service, stack=stack)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT value 
                FROM tenant_configs
                JOIN tenant on tenant_configs.tenant_id = tenant._id
                WHERE tenant.name = %s AND category = 'datasets' AND config_name = 'OppDS'
                """,
                (tenant_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"No OppDS config found for tenant: {tenant_name}")
    finally:
        conn.close()
    try:
        oppds_raw_config = json.loads(row["value"]) # this is stored as a string in the db
    except json.JSONDecodeError:
        raise ValueError(f"Invalid JSON found in OppDS config for tenant: {tenant_name}")
    except Exception as e:
        raise ValueError(f"Unexpected error while parsing OppDS config for tenant: {tenant_name}. Error: {str(e)}")
    return oppds_raw_config


class GbmTenantConfigReader:
    """Read-only access to one tenant's configs and flags in the GBM microservice Postgres.

    Legacy gbm-service reads tenant configs (``sec_context.details.get_config``) and flags
    (``sec_context.details.get_flag``) from its own Postgres. In aviso-core ``sec_context.details`` is the
    FM Postgres, so this reads the same ``tenant_configs`` / ``tenant_flags`` rows from the tenant's GBM
    stack (gbm1-ms, gbm2-ms, ...), resolved the same way as ``get_oppds_config``. ``get_config`` and
    ``get_flag`` follow legacy ``Tenant.get_config`` / ``Tenant.get_flag`` (without sandbox support).

    Read-only by construction:

    * only the two SELECT statements in ``_SELECT`` are ever executed;
    * the connection runs with ``readonly=True`` and autocommit off, so psycopg2 opens every transaction
      with ``BEGIN READ ONLY`` and Postgres rejects any write. That is transaction-scoped, so nothing
      leaks into pooled (pgbouncer) server sessions;
    * each read ends its transaction straight away (rollback), so no session sits idle in a transaction.

    One connection per instance, opened on first read and closed by ``close()`` or the ``with`` block.
    """

    _SELECT = {
        'tenant_configs': (
            "SELECT tenant_configs.value FROM tenant_configs "
            "JOIN tenant ON tenant_configs.tenant_id = tenant._id "
            "WHERE tenant.name = %s AND tenant_configs.category = %s AND tenant_configs.config_name = %s"
        ),
        'tenant_flags': (
            "SELECT tenant_flags.value FROM tenant_flags "
            "JOIN tenant ON tenant_flags.tenant_id = tenant._id "
            "WHERE tenant.name = %s AND tenant_flags.category = %s AND tenant_flags.config_name = %s"
        ),
    }

    def __init__(self, tenant_name):
        self.tenant_name = tenant_name
        self.stack = None
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                logger.info("Failed to close GBM Postgres connection: %s", e)

    def _connection(self):
        if self._conn is None:
            # _get_ms_stack re-initialises ConnectionFactory's shared Postgres manager; ConnectionFactory
            # itself only calls it under the per-db_type lock, so do the same here.
            with ConnectionFactory._locks['gbm']:
                stack = ConnectionFactory._get_ms_stack(self.tenant_name, 'gbm')
            conn = ConnectionFactory._get_pg_conn(db_type='gbm', stack=stack)
            try:
                conn.set_session(readonly=True, autocommit=False)
            except Exception:
                conn.close()
                raise
            self._conn, self.stack = conn, stack
            logger.info("GBM tenant config reader: tenant=%s gbm stack=%s", self.tenant_name, stack)
        return self._conn

    def _fetch_value(self, table, category, config_name):
        """Return ``(value, found)``; ``found`` is False when there is no row (legacy returns the default)."""
        conn = self._connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(self._SELECT[table], (self.tenant_name, category, config_name))
                row = cur.fetchone()
        finally:
            conn.rollback()
        if row is None:
            return None, False
        # stored as a JSON string, decoded the same way as legacy TenantBaseConfiguration.decode
        return json.loads(row['value']), True

    def get_config(self, category, config_name=None, default=None):
        """Legacy ``Tenant.get_config(category, config_name, default)`` for ``sandbox=None``."""
        if not config_name:
            raise NotImplementedError("Reading a whole config category is not supported")
        if '.' in config_name:
            try:
                conf_name = config_name.split('.')[0]
                current_config = self.get_config(category, conf_name, default)
                return self.find_in_config({conf_name: current_config}, config_name)
            except KeyError:
                return default
        value, found = self._fetch_value('tenant_configs', category, config_name)
        if found:
            return value
        else:
            return default

    def get_flag(self, flagset, flag, default=None):
        """Legacy ``Tenant.get_flag(flagset, flag, default)``."""
        value, found = self._fetch_value('tenant_flags', flagset, flag)
        if found:
            return value
        else:
            if default is not None:
                return default
            else:
                raise GnanaError("Key Not Found!!!")

    @staticmethod
    def find_in_config(current_config, config_path):
        """Legacy ``Tenant.find_in_config``: the value at a dotted path, otherwise ``KeyError``."""
        keys = config_path.split('.')
        f_val = current_config
        for key in keys:
            try:
                f_val = f_val[key]
            except KeyError:
                raise
        return f_val