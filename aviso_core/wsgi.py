"""
WSGI config for aviso_core project.
"""
# TODO: remove this whole shim. It exists only to drag the PyPI `pyschema==2.4.0`
# -- Spotify's Avro library, Python 2 only, whose core does `from itertools
# import izip` -- into Python 3. That package is no longer a dependency: Aviso's
# real schema library is now vendored at aviso/utils/pyschema.py in
# aviso-infrastructure, and the py-schema requirement has been dropped from
# requirements.txt.
#
# Safe to leave in the meantime. The pyschema block below is guarded on
# `submodule_search_locations`, which is non-None only for a package directory;
# the vendored module is not even a top-level module, so find_spec("pyschema")
# returns None and the block cannot execute. The itertools assignments are inert
# attribute additions on the stdlib.
#
# Kept only until a deployed run confirms nothing else relied on the zipimport
# patch below, which is NOT pyschema-specific and needs its own look.
import itertools
if not hasattr(itertools, "izip"):
    itertools.izip = zip
import os
import sys
import zipimport
import itertools
import importlib.util

if not hasattr(itertools, "izip"):
    itertools.izip = zip

if not hasattr(itertools, "ifilter"):
    itertools.ifilter = filter

if not hasattr(itertools, "ifilterfalse"):
    itertools.ifilterfalse = itertools.filterfalse

if not hasattr(itertools, "imap"):
    itertools.imap = map

pyschema_spec = importlib.util.find_spec("pyschema")

if pyschema_spec and pyschema_spec.submodule_search_locations:
    pyschema_path = pyschema_spec.submodule_search_locations[0]
    core_path = os.path.join(pyschema_path, 'core.py')

    if os.path.exists(core_path):
        spec = importlib.util.spec_from_file_location("core", core_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["core"] = module
        spec.loader.exec_module(module)

class PatchedZipImporter(zipimport.zipimporter):
    def __init__(self, path):
        if path and path not in sys.path:
            sys.path.insert(0, path)
        try:
            super().__init__(path)
        except Exception:
            pass

    def find_spec(self, fullname, path=None, target=None):
        return None

zipimport.zipimporter = PatchedZipImporter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aviso_core.settings")

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()