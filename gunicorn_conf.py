"""Gunicorn settings; listen address comes from the authoritative SQLite DB."""
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from manage import read_web_configuration
from nginx_proxy import backend_web

web = backend_web(CODE_ROOT, read_web_configuration())
host = f"[{web['host']}]" if ":" in web["host"] else web["host"]
bind = f"{host}:{web['port']}"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
preload_app = False
daemon = False
umask = 0o077
worker_tmp_dir = "/tmp"
