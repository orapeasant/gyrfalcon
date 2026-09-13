"""Authentication and the identity store.

Spec: 15-flow.md §17.11 step 8.

Division of labour, decided deliberately: the IdP (Entra) proves *who someone
is*; gyrfalcon owns *what they belong to and may do*. Orgs, membership and
roles are ours. That costs a second place where access has to be kept current,
and buys three things — role changes do not need a directory administrator,
one deployment can serve several customers, and an organization survives a
change of identity provider.
"""

from gyrfalcon.auth.keys import format_key, hash_key, mint_key, split_key
from gyrfalcon.auth.store import AuthStore, get_auth_store, set_auth_store

__all__ = [
    "AuthStore",
    "format_key",
    "get_auth_store",
    "hash_key",
    "mint_key",
    "set_auth_store",
    "split_key",
]
