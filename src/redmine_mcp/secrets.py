"""Load Redmine credentials from environment variables.

Supports two auth shapes:

* API key — env var ``REDMINE_API_KEY``
* OAuth2 bearer token (Doorkeeper, Redmine 6.1+) — env var ``REDMINE_OAUTH_TOKEN``

Secrets are expected to be set in the shell environment (e.g. via
``~/.bash_secrets`` sourced by ``~/.bashrc``). The canonical secret store
is ``/opt/simsyssecrets/vault.enc.yaml`` (SOPS+age encrypted).
"""

from __future__ import annotations

import os


def load_api_key(
    secrets_file: object = None,
    *,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve the Redmine API key from environment.

    The ``secrets_file`` parameter is accepted for back-compat but ignored.
    """
    e = env if env is not None else os.environ
    direct = e.get("REDMINE_API_KEY")
    if direct:
        return direct.strip()
    return None


def load_oauth_token(
    secrets_file: object = None,
    *,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve the Redmine OAuth2 bearer token from environment.

    The ``secrets_file`` parameter is accepted for back-compat but ignored.
    """
    e = env if env is not None else os.environ
    direct = e.get("REDMINE_OAUTH_TOKEN")
    if direct:
        return direct.strip()
    return None
