"""GBM hierarchy sync for ``/gbm/hier_metadata`` and ``/gbm/hier_sync``.

Ported from legacy gbm-service ``gbm/services/sync_service.py``. ``hier_indexes``, ``iter_chunks``,
``sync_hierarchy`` and ``get_hier_metadata`` are verbatim, with two changes:

1. Where the default ``db`` comes from:

   * legacy: ``sec_context.tenant_db``. In gbm-service that is the tenant's **GBM** Mongo DB.
   * here: ``_gbm_tenant_db()``, i.e. ``ConnectionFactory.get_mongo_db(..., db_type='gbm')``, the same DB
     (the same ``tenant_db_url`` from the GBM Postgres, decrypted with the GBM key).

   Do NOT use aviso-core's ``sec_context.tenant_db`` here: in aviso-core that is the tenant's **FM** (app)
   Mongo DB, which holds the app-side hierarchy (the source of truth the SDK reads and sends to
   hier_sync). ``sync_hierarchy`` drops the collection it is given, so pointing it at FM would delete
   the source of truth.

2. ``sync_hierarchy`` first calls ``validate_hier_records``. It refuses, before anything is dropped, a
   payload that legacy would only fail on after the drop (leaving GBM's hierarchy empty or partial): an
   empty list, something that is not a list, a record that is not an object, or a record that cannot be
   encoded to BSON. Every payload that legacy stores without raising passes unchanged.

``get_hier_metadata`` is read-only (one ``find``). ``sync_hierarchy`` is destructive: it drops the tenant's
GBM ``hierarchy`` collection, recreates its indexes and inserts the records, exactly as legacy does.
"""
import hashlib
import json
import logging
import os
import time

import bson
from pymongo import InsertOne, IndexModel, DESCENDING
from pymongo.errors import BulkWriteError

from aviso.framework.connection_factory import ConnectionFactory
from aviso.settings import sec_context

logger = logging.getLogger('gnana.%s' % __name__)

HIER_COLL = 'hierarchy'

hier_indexes = [
        IndexModel([
            ('node', DESCENDING),
            ('parent', DESCENDING),
            ('from', DESCENDING),
            ('to', DESCENDING)], unique=True),
        IndexModel([
            ('node', DESCENDING),
            ('hidden', DESCENDING),
            ('from', DESCENDING),
            ('to', DESCENDING)]),
        IndexModel([
            ('parent', DESCENDING),
            ('hidden', DESCENDING),
            ('from', DESCENDING),
            ('to', DESCENDING)]),
        ]


class InvalidHierarchyPayload(ValueError):
    """The hier_sync payload was refused before anything was written."""


def _gbm_tenant_db():
    """The tenant's GBM Mongo DB: what legacy gbm-service's ``sec_context.tenant_db`` is."""
    cname = os.environ.get("CNAME", "preprod")
    return ConnectionFactory.get_mongo_db(tenant=sec_context.name, db_type='gbm', cname=cname)


def validate_hier_records(records):
    '''
    Refuses, before the GBM hierarchy is dropped, a payload that legacy sync_hierarchy would only fail on
    after the drop. Raises InvalidHierarchyPayload; returns None when the payload can be stored.

    The checks mirror what pymongo does when it inserts (``InsertOne`` + ``bulk_write``):
    * not a list:  legacy iterates it and ``bulk_write`` raises TypeError
    * empty list:  legacy's single batch is empty and ``bulk_write([])`` raises InvalidOperation
    * a record that is not an object:  ``bulk_write`` raises TypeError ("document must be an instance of dict")
    * a record that cannot be BSON-encoded (e.g. an integer beyond 64 bits):  the encoder raises.
      ``bson.encode`` with ``check_keys=False`` is the same encoder and key check pymongo 3.12 uses for
      inserts, so a record pymongo would store always passes.
    '''
    if not isinstance(records, list):
        raise InvalidHierarchyPayload(
            'expected a JSON list of hierarchy records, got %s' % type(records).__name__)
    if not records:
        raise InvalidHierarchyPayload(
            'empty list of hierarchy records: refusing to drop the GBM hierarchy and leave it empty')
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise InvalidHierarchyPayload(
                'record %d is %s, expected a JSON object' % (idx, type(rec).__name__))
        try:
            bson.encode(rec, check_keys=False)
        except Exception as e:
            raise InvalidHierarchyPayload(
                'record %d cannot be stored in Mongo: %s: %s' % (idx, type(e).__name__, e))


def iter_chunks(my_list, batch_size=5):
    start = 0
    more = True
    while more:
        end = start + batch_size
        if end < len(my_list):
            yield my_list[start:end]
            start = end
        else:
            more = False
            yield my_list[start:]


def sync_hierarchy(records, db=None):
    '''
    Syncs the hierarchy in the gbm db to be used by the CoolerHierarchyService (zero-dawn)

    Arguments:
        records {} - A list of new style hierarchy records
        db {} - At some point we should probably make this unit-testable, in which case we'll pass a db conn

    Returns:
        The metadata required to validate the new hierarchy
    '''
    start = time.time()
    # Not in legacy: refuse a payload that would fail after the drop (see validate_hier_records), before
    # the GBM DB is even resolved.
    validate_hier_records(records)
    # legacy: `db if db else sec_context.tenant_db` (the GBM DB in gbm-service); see the module docstring.
    db = db if db is not None else _gbm_tenant_db()
    db.drop_collection(HIER_COLL)
    db[HIER_COLL].create_indexes(hier_indexes)
    operations = [InsertOne(rec) for rec in records]
    for batch in iter_chunks(operations, 1000):
        try:
            db[HIER_COLL].bulk_write(batch, ordered=False, bypass_document_validation=True)
        except BulkWriteError as bwe:
            logger.error(bwe.details)
    logger.warning('Hier Sync Time: {}'.format(time.time() - start))
    return get_hier_metadata(db)


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
