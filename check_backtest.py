"""Read-only Flask HTTP GET smoke test for the installed backtest screen.

Runs through Flask's real request dispatch and authentication middleware with
an isolated authenticated test session. Never submits forms, downloads history,
or accesses the internet. Intended for the install/update health gate.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import traceback

from app import create_app


def check(root: str, run_id: str = "") -> int:
    try:
        flask_app = create_app(root)
        flask_app.testing = True
        flask_app.config["PROPAGATE_EXCEPTIONS"] = True
        store = flask_app.extensions["futures_store"]
        with store.read() as con:
            auth = store.setting(con, "auth", {})
            latest = con.execute(
                "SELECT id FROM backtests ORDER BY time DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not auth or "version" not in auth:
            raise RuntimeError("Login is not yet configured; cannot test authenticated GET")
        paths = ["/backtest"]
        selected_run_id = run_id or (latest["id"] if latest else "")
        if selected_run_id:
            from urllib.parse import quote
            paths.append("/backtest?run_id=" + quote(selected_run_id, safe=""))
        with flask_app.test_client() as client:
            with client.session_transaction(base_url="https://localhost") as sess:
                sess["login_method"] = "password"
                sess["auth_version"] = auth["version"]
                sess["csrf"] = "backtest-smoke-test"
            for path in paths:
                response = client.get(path, base_url="https://localhost", follow_redirects=False)
                html = response.get_data(as_text=True)
                if response.status_code != 200 or "main-backtest-form" not in html:
                    raise RuntimeError(
                        f"GET {path}: status={response.status_code}, "
                        f"form_present={'main-backtest-form' in html}"
                    )
                print(f"BACKTEST_GET_OK path={path} bytes={len(response.data)}")
        return 0
    except Exception:
        print("BACKTEST_GET_FAILED: traceback follows", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/root/futures")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    sys.exit(check(str(Path(args.root).resolve()), args.run_id))
