"""One choke point that strips key-shaped text out of a record before it is stored.

`data/` is committed and append-only (docs/adr/0001-git-as-transport.md), so a
credential that lands in a day file is in the history for good. There is no
fixing that forward, which makes it the one class of bug worth paying for
twice.

It has now been paid for twice. Two separate attempts to keep a key out of the
store were made at the site that produced the text — first a provider's
response body, then a key pasted into `VENATOR_LLM_API_KEY_VAR` — and each one
reasoned carefully about the risk and still left a route open. A guard at the
producer has to be got right once per route, forever, by everyone who ever adds
a route. The store cannot forgive a miss.

So this is the other half of that defence and it sits at the *writer*. Every
heartbeat line is serialised and then passed through `scrub` on its way to
disk, so a route nobody has thought of yet is still covered — it has to pass
through the writer to reach the file.

This does not replace constructing failures that cannot leak. `venator.llm`'s
key lane still builds its messages out of integers and exception class names,
and that stays the primary guarantee, because a message that never contains a
key cannot be got wrong by a pattern that misses. This is the net under it.

Deliberately biased toward over-redaction. A diagnostic that loses a git SHA to
an over-eager pattern costs somebody a minute of squinting; a key in a
committed file costs a key rotation and an unrewritable history.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# One JSON-shaped record: a heartbeat before it is serialised.
type Record = str | int | float | bool | None | Sequence[Record] | Mapping[str, Record]

REDACTED = "<redacted>"

# Where a token is allowed to start. `\b` was the obvious choice and it is
# wrong twice over, because a word character immediately before a token makes
# every pattern below stop matching.
#
# It stops matching on a character the writer invents. `json.dumps` renders a
# newline as the two characters `\` and `n`, and `\u00e9` for an accented
# letter, so a key that began the second line of a runtime's stderr reached
# the pattern glued to the letter `n` and was written out whole. Measured on
# both writers, with a Groq key and with a JWT.
#
# It also stops matching on a real one: a token abutting a non-ASCII word is
# still a token. Excluding only the ASCII word characters covers both and
# gives up nothing, because it is the ASCII ones that ordinary text is made
# of: "risk-management-notes" is left alone here exactly as `\b` left it.
NOT_AFTER_A_WORD = r"(?<![A-Za-z0-9_])"

# Credentials embedded in a URL's userinfo, e.g. a corporate proxy written as
# http://someone:hunter2@proxy.example:8080. Handled first and on its own,
# because the interesting half is the password and it has no fixed shape.
URL_CREDENTIALS = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")

# Vendor token prefixes, which is what most keys actually look like in the wild.
# Anthropic and OpenAI sk-/rk-, Groq gsk_, Hugging Face hf_, GitHub ghp_/gho_/
# ghu_/ghs_/ghr_ and github_pat_, Slack xox*, Shopify shpat_/shppa_.
PREFIXED_TOKEN = re.compile(
    NOT_AFTER_A_WORD
    + r"(?:sk|rk|pk|gsk|hf|ghp|gho|ghu|ghs|ghr|xoxb|xoxp|xoxa|xoxs|xoxr|shpat|shppa)"
    r"[-_][A-Za-z0-9_\-]{12,}"
)

# Token shapes with their own alphabet rather than a separator.
VENDOR_TOKEN = re.compile(
    NOT_AFTER_A_WORD
    + r"(?:github_pat_[A-Za-z0-9_]{12,}"                 # GitHub fine-grained
    r"|AKIA[0-9A-Z]{12,}"                                # AWS access key id
    r"|ASIA[0-9A-Z]{12,}"                                # AWS temporary access key id
    r"|AIza[A-Za-z0-9_\-]{20,}"                          # Google
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"  # JWT
    r")"
)

# A credential presented in a header or handed to a flag.
AUTH_VALUE = re.compile(
    r"(?P<prefix>" + NOT_AFTER_A_WORD + r"(?:Bearer|Basic|Token)\s+)"
    r"[A-Za-z0-9_\-.=+/]{12,}",
    re.IGNORECASE,
)

# A credential written as a name/value pair, in a query string, a JSON body or
# a shell line. The name is kept so the diagnostic still says what was set.
NAMED_SECRET = re.compile(
    r"(?P<prefix>"
    + NOT_AFTER_A_WORD
    + r"\"?(?:api[-_]?key|access[-_]?token|auth[-_]?token|secret[-_]?key"
    r"|client[-_]?secret|password|passwd|secret|token|apikey|key)\"?\s*[=:]\s*\"?)"
    r"[A-Za-z0-9_\-.=+/]{8,}",
    re.IGNORECASE,
)

# The unprefixed case the vendor list cannot cover: Azure, Together and
# Fireworks all issue bare 32-to-64-character hex or alphanumeric keys. Nothing
# legitimate in a heartbeat is a 32-character unbroken run of letters and
# digits, with one exception — a git SHA in an error message — and losing that
# is the cheap side of this trade.
BARE_TOKEN = re.compile(NOT_AFTER_A_WORD + r"[A-Za-z0-9]{32,}(?![A-Za-z0-9_])")

_PATTERNS = (PREFIXED_TOKEN, VENDOR_TOKEN, AUTH_VALUE, NAMED_SECRET, BARE_TOKEN)


def scrub_record(record: Record) -> Record:
    """Scrub every string in a record before it is serialised.

    This runs first and `scrub` still runs on the finished line, because the two
    catch different things. Serialising first is what let a key hide: escaping
    replaces a newline with `\\n`, and the patterns then see a word character
    where the text had a line break. Scrubbing the values first means they are
    matched against the characters the runtime actually produced.

    `scrub` on the serialised line stays as the last thing before the bytes
    reach disk — this walk only knows about strings, lists and mappings, and the
    boundary must not depend on a future record being made of those.
    """
    if isinstance(record, str):
        return scrub(record)
    if isinstance(record, Mapping):
        return {
            scrub(name) if isinstance(name, str) else name: scrub_record(value)
            for name, value in record.items()
        }
    if isinstance(record, Sequence) and not isinstance(record, (str, bytes)):
        return [scrub_record(item) for item in record]
    return record


def scrub(text: str) -> str:
    """Replace anything key-shaped with `<redacted>`.

    Removal and replacement only: no pattern here adds characters that were not
    already structure, so scrubbing an already-serialised JSON line leaves it
    valid JSON. `<redacted>` carries no quote, backslash or control character,
    which is what makes that safe.
    """
    scrubbed = URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", text)
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(_replacement, scrubbed)
    return scrubbed


def _replacement(match: re.Match[str]) -> str:
    """Keep whatever names the secret, drop whatever is the secret."""
    named = match.groupdict().get("prefix")
    return f"{named}{REDACTED}" if named else REDACTED
