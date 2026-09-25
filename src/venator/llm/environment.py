"""The environment a runtime's child process is allowed to see.

Both command-line runtimes prefer a key found in the environment over the login
the person made themselves. A stale ANTHROPIC_API_KEY or OPENAI_API_KEY left in
a shell — exported months ago, or by some unrelated tool — therefore redirects
the spend to an API account silently: nothing on screen says which account paid.
Inheriting os.environ makes that a matter of luck. This module makes it a matter
of code: a child sees the variables named here and nothing else, so the
subscription lanes are structurally unable to pick a key up.

tests/llm/test_environment.py plants both keys and proves neither survives.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

# Everything a runtime legitimately needs to start, find the person's own login,
# resolve a host and verify a certificate — and nothing that carries an identity
# to a vendor. HOME is here because the unmodified binary reads its own
# subscription login from under it; this adapter never opens that login itself.
#
# CLAUDE_CONFIG_DIR is here because that same login moves with it. Anthropic
# documents the variable as relocating the whole configuration directory —
# "default: `~/.claude`. All settings, session history, and plugins are stored
# under this path, as are credentials on Linux and Windows"
# (https://code.claude.com/docs/en/env-vars). Someone who has set it signs in to
# one directory while a child that cannot see the variable reads another, and
# the run fails as "not signed in" with nothing on screen pointing at the
# cause. It is in this tier rather than the Windows one because the person sets
# it themselves on every platform, and the documented Linux behaviour is exactly
# the case a Windows-only entry would leave broken.
#
# "The whole configuration directory" is more than the credential, and the
# difference is worth stating rather than glossing: a settings file lives there
# too, and a settings file may carry an `env` block — applied to the CLI's own
# process, over what the shell exported, in `-p` mode as much as any other — or
# an `apiKeyHelper` that mints a key. Honouring this name therefore lets that
# directory's settings reach the child. What makes it acceptable is that it is
# not new reach: HOME is already on this list, the default directory is
# `~/.claude` under it, and a settings file there has exactly the same say.
# This entry changes *which* directory the child reads, never what a
# configuration directory is allowed to do.
#
# Its value is passed through verbatim, and nothing in this module checks it —
# zero guards, not a weak one. DENIED matches variable *names*, and this name is
# a compile-time literal that is not credential-shaped, so DENIED can never fire
# on it. That is a decision rather than an oversight. The accident this module
# exists to prevent is a key left in a shell quietly outbidding the login the
# person made themselves; a relocated configuration directory is the opposite of
# that, being how the child finds that login at all. Anything Venator refused or
# rewrote here would put the child back to reading a different directory than
# the person's own `claude` does, which is the defect this entry exists to fix.
# The one behaviour that does apply is the truthiness filter in
# child_environment: a blank value is dropped, so the child falls back to its
# own documented default rather than resolving an empty path.
#
# The proxy variables at the end are a deliberate judgment call, and the one
# entry in this list that touches *where traffic goes* rather than what a
# process can read. They are here because a person behind a corporate proxy
# cannot reach a vendor at all without them, and stripping them would turn this
# allowlist into "the local LLM runtime cannot reach the vendor at the office". The safeguard is that
# they are only ever **inherited** — read from the shell that started the run,
# never written, defaulted, or rewritten here — so a run goes exactly where that
# person's own shell was already sending everything else. Nothing in Venator
# points anyone's traffic anywhere. If that trade ever stops looking right, the
# fix is to delete these eight names, not to start setting them. This comment
# records the reasoning so the next reader does not have to rediscover it.
ALLOWED: tuple[str, ...] = (
    "PATH",
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

# The same list, for Windows — which sets almost none of the names above.
#
# On a stock Windows box every POSIX name in ALLOWED except PATH is unset, so
# the child environment was `{"PATH": ...}` and nothing else. That is not a
# hardened environment, it is a broken one: CPython's own test suite records
# that "Windows requires at least the SYSTEMROOT environment variable to start
# Python" and skips its empty-env test on win32 for exactly that reason, and a
# Node runtime with no SystemRoot cannot initialise its socket layer either.
#
# This tier only ever **adds**, and only machine facts: where the OS lives, what
# the command interpreter is, which extensions are executable, where scratch
# files go, where this account's own directories are, and how many CPUs there
# are. Every one of them is something the child could read off the machine if it
# had to. None of them is a credential, none names a vendor, and none changes
# where traffic goes — the proxy entries above remain the only names in this
# module that do, and they are untouched here. DENIED still applies to both
# tiers on the way out; tests/llm/test_environment.py proves a planted
# ANTHROPIC_API_KEY / OPENAI_API_KEY survives neither.
#
# Spelled in upper case on purpose, and it matters. Windows environment names
# are case-insensitive and `os.environ` is an `os._Environ` whose `encodekey`
# upper-cases every key, so `source.get("SYSTEMROOT")` finds the variable
# Windows itself spells `SystemRoot`. A plain dict handed in through the
# `parent` seam is *not* case-folding, so a caller or a test that passes
# `{"SystemRoot": ...}` gets nothing — which would look like a passing fix and
# be a no-op. Production always passes `os.environ`; tests must fold, or spell
# the keys in upper case.
#
# Deliberately not here: PUBLIC and PROGRAMFILES(X86), which nothing this
# adapter spawns has been shown to need. Add a name when a child is actually
# observed to need it, not in advance.
WINDOWS_ALLOWED: tuple[str, ...] = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    "NUMBER_OF_PROCESSORS",
    "OS",
)

# Belt to the allowlist's braces. An allowlist is only as good as the care taken
# when someone appends to it, and the failure mode of a careless append is an
# invisible bill rather than a broken run — so a credential-shaped name is
# refused on the way out even if it is listed above, and refused loudly if a
# caller hands one in as an extra.
DENIED = re.compile(
    r"api[_-]?key|auth[_-]?token|access[_-]?token|session[_-]?key"
    r"|base[_-]?url|credential|secret|password|passwd|oauth|bearer",
    re.IGNORECASE,
)


def allowed_names(os_name: str | None = None) -> tuple[str, ...]:
    """The allowlist for one platform: the POSIX tier, plus Windows' if this is it.

    A platform split rather than one union, so a name that only makes sense on
    Windows cannot cross the boundary on a machine where something else could
    have set it. `os_name` takes the same values `os.name` does and exists so a
    test can exercise the Windows tier without Windows.
    """
    name = os.name if os_name is None else os_name
    return (*ALLOWED, *WINDOWS_ALLOWED) if name == "nt" else ALLOWED


def child_environment(
    parent: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> dict[str, str]:
    """Build a child process environment from the allowlist, never by inheriting.

    `extra` is for the adapter's own non-secret settings (a model name, a flag).
    A credential-shaped name there is a mistake worth an exception: this adapter
    never carries a vendor credential across the process boundary.
    """
    source = os.environ if parent is None else parent
    child = {
        name: source[name]
        for name in allowed_names(os_name)
        if source.get(name)
    }
    for name, value in (extra or {}).items():
        if DENIED.search(name):
            raise ValueError(
                f"refusing to hand {name} to a runtime: this adapter never "
                "carries vendor credentials into a child process"
            )
        child[name] = value
    return {name: value for name, value in child.items() if not DENIED.search(name)}
