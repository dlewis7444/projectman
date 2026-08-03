"""Terminal URI matching helpers.

Pure module, no GTK/Vte imports, so link-shape behavior is testable headlessly.
"""

import re


# RFC 3986 scheme followed by // and a non-whitespace path. Keeping the //
# requirement avoids treating ordinary text like "Note: this" as a link.
URI_SCHEME_PATTERN = r'\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+'

_URI_SCHEME_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9+.-]*)://')


def uri_scheme(uri: str) -> str | None:
    """Return the lowercase scheme for a // URI, or None."""
    match = _URI_SCHEME_RE.match(uri)
    return match.group(1).lower() if match else None
