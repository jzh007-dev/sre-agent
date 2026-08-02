"""Credential resolution — construction layer.

Small, but the error message matters. A missing key surfaces as a wall of
provider-SDK stack trace by default, and the most common first-run failure in a
project like this is "which environment variable was I supposed to set". The
message here names the variable, the provider, and the file to put it in.

`AuthError` in `errors.py` covers a key that exists and is *wrong*; this module
covers a key that is absent, which is a wiring failure and should stop the process
rather than be retried or failed over.
"""
from __future__ import annotations

import os

from .provider_catalog import PROVIDERS, ProviderSpec


class MissingCredential(RuntimeError):
    """A provider was routed to but has no API key configured."""


def api_key(spec: ProviderSpec) -> str:
    value = os.environ.get(spec.api_key_env, "").strip()
    if not value:
        raise MissingCredential(
            f"{spec.name} is configured but ${spec.api_key_env} is not set. "
            f"Add it to .env (see .env.example) or export it. "
            f"Providers currently known: {sorted(PROVIDERS)}"
        )
    return value


def configured_providers() -> list[str]:
    """Provider names with a key present.

    Used at wiring time so the gateway can refuse a fallback chain whose members
    have no credentials — a fallback candidate that will always fail with
    `MissingCredential` is worse than no candidate, because it burns an attempt
    and looks like a provider outage in the trace.
    """
    return [name for name, spec in PROVIDERS.items() if os.environ.get(spec.api_key_env, "").strip()]
