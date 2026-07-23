"""Command allow/deny filtering logic.

Uses deny-first, then allow, then default-deny approach.
Glob matching is case-insensitive using fnmatch.
"""

import fnmatch
import os.path
import re
import shlex


def glob_match(command: str, pattern: str) -> bool:
    """Match a command string against a glob pattern (case-insensitive).

    Pattern examples:
        '*.statistics'       -> matches 'cache.statistics', 'dns.statistics'
        'querystore.*'       -> matches 'querystore.top-clients', 'querystore.count'
        'dns.config show *'  -> matches 'dns.config show zones'
    """
    return fnmatch.fnmatch(command.lower(), pattern.lower())


def check_command(command: str, rules: dict) -> tuple[bool, str]:
    """Check if a command is allowed by the deny/allow rules.

    An empty command means "invoke the tool with no arguments" — legitimate
    for `uptime`, `free`, or a bare pipe stage like `| wc`. It is matched
    against the patterns like any other command rather than being rejected
    up front, so `allow: ["*"]` permits it while `allow: ["ps *"]` does not.
    The authorization decision stays with the catalog author.

    Returns:
        (allowed, reason) — True if allowed, False if denied.
    """
    cmd = command.strip()

    for pattern in rules.get("deny", []):
        if glob_match(cmd, pattern):
            return False, f"Command matches deny rule: {pattern}"

    for pattern in rules.get("allow", []):
        if glob_match(cmd, pattern):
            return True, "OK"

    return False, "Command does not match any allow rule"


def _is_path_shaped(s: str) -> bool:
    return s.startswith("/") or s.startswith("./") or s.startswith("../")


def _normalize_path(path: str) -> str:
    """Collapse a path to canonical lexical form.

    Purely lexical — never touches the filesystem, so there is no TOCTOU
    window between this check and exec, and no dependence on the server's
    mount view. Symlinks are therefore NOT resolved.

    os.path.normpath alone is not enough: POSIX mandates that exactly two
    leading slashes are preserved, so normpath('//etc/shadow') is a no-op.
    Collapse the leading run first.
    """
    return os.path.normpath(re.sub(r"^/+", "/", path))


def _candidate_paths(token: str) -> list[str]:
    """Path-shaped strings hiding inside a single argv token.

    Bare tokens ('/etc/shadow') are the obvious case, but paths also ride in
    attached to flags — '--file=/etc/shadow', '-f/etc/shadow' — where they
    would otherwise never be examined.
    """
    candidates: list[str] = []

    if _is_path_shaped(token):
        candidates.append(token)

    # key=/path, including --long=/path
    if "=" in token:
        rhs = token.split("=", 1)[1]
        if _is_path_shaped(rhs):
            candidates.append(rhs)

    # -f/path — path attached directly to a short flag
    if token.startswith("-"):
        slash = token.find("/")
        if slash > 0:
            candidates.append(token[slash:])

    return candidates


def check_paths(args: str, deny: list[str]) -> tuple[bool, str]:
    """Reject any path-shaped token in `args` that matches a deny pattern.

    Paths are matched both as written and in normalized form, so a deny rule
    written against the literal spelling still fires while '//etc', '/etc/./'
    and '/etc/../' traversals are also caught.

    Returns (True, "OK") if nothing matches; (False, reason) on first match.
    Pattern matching is case-insensitive glob via fnmatch.
    """
    if not deny:
        return True, "OK"
    try:
        tokens = shlex.split(args)
    except ValueError as e:
        return False, f"path check: failed to tokenize args ({e})"

    for tok in tokens:
        for candidate in _candidate_paths(tok):
            forms = {candidate, _normalize_path(candidate)}
            for pattern in deny:
                if any(glob_match(form, pattern) for form in forms):
                    return False, f"path matches deny rule: {pattern}"
    return True, "OK"
