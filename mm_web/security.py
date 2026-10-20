"""Web compatibility imports for the shared, stdlib-only secret filter."""
from secret_filter import (  # noqa: F401
    SENSITIVE, HMAC_VALUE, HEX, CREDENTIAL, BEARER, JSON_SECRET,
    decode_unicode, sensitive, scrub,
)
