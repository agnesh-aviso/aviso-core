"""GBM hierarchy metadata for ``/gbm/hier_metadata``.

Ported from legacy gbm-service ``gbm/services/sync_service.py`` (``get_hier_metadata``). The function
body is verbatim. The only change is where the default ``db`` comes from:

* legacy: ``sec_context.tenant_db``. In gbm-service that is the tenant's **GBM** Mongo DB.
* here: ``_gbm_tenant_db()``, i.e. ``ConnectionFactory.get_mongo_db(..., db_type='gbm')``, the same DB
  (the same ``tenant_db_url`` from the GBM Postgres, decrypted with the GBM key).

Do NOT use aviso-core's ``sec_context.tenant_db`` here: in aviso-core that is the tenant's **FM** (app)
Mongo DB, which holds the app-side hierarchy the SDK compares this checksum against. Reading it would
make the checksum always match, so GBM's hierarchy would silently never be re-synced.

Read-only: one ``find`` on the ``hierarchy`` collection, nothing is written.
"""
import hashlib
import json
import logging
import os
import time

from aviso.framework.connection_factory import ConnectionFactory
from aviso.settings import sec_context

logger = logging.getLogger('gnana.%s' % __name__)

HIER_COLL = 'hierarchy'


def _gbm_tenant_db():
    """The tenant's GBM Mongo DB: what legacy gbm-service's ``sec_context.tenant_db`` is."""
    cname = os.environ.get("CNAME", "preprod")
    return ConnectionFactory.get_mongo_db(tenant=sec_context.name, db_type='gbm', cname=cname)


def get_hier_metadata(db=None):
    '''
    Retrieves the metadata currently stored in the hierarchy collection
    '''
    start = time.time()
    # legacy: `db if db else sec_context.tenant_db`. `is not None` is the same for any pymongo Database
    # (pymongo 3.x Databases are always truthy; pymongo 4 raises on bool(Database)).
    db = db if db is not None else _gbm_tenant_db()
    # No sort and no extra cursor options, exactly as legacy: the checksum depends on the natural order
    # and on json.dumps defaults, and must match the SDK's checksum of the app-side collection
    # (aviso.sdk.common.utils.create_collection_checksum). A mismatch makes the SDK call /gbm/hier_sync,
    # which drops and rewrites the GBM hierarchy.
    recs = list(db[HIER_COLL].find({}, {'_id': 0}))
    output = hashlib.sha1(bytes(json.dumps(recs), 'utf-8')).hexdigest()
    logger.warning('Hier Meta Time: {}'.format(time.time() - start))
    return output
