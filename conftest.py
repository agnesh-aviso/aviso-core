"""pytest setup shared by every test under aviso-core.

aviso_core.settings refuses to load without SECRET_KEY, and pytest does not
read .env (manage.py does). The tests only need some key to sign their own
session cookies, so give them a throwaway one unless a real one is already set.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-only-not-a-real-secret-key")
