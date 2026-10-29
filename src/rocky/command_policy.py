"""Side-effect-free matching of simple shell commands against owner approvals.

Prefix approval is only meaningful for a *simple* command: one program and its
words. Anything the shell could use to chain, redirect, substitute or expand
into another command makes a command ineligible, so it always needs exact
approval. Ineligible text never matches; it is not "cleaned" or reinterpreted.
"""

import re
import shlex
from pathlib import PurePosixPath

_OPERATORS = frozenset("();<>|&")
# Control characters include newlines, which separate shell commands.
# $ and backticks perform parameter, arithmetic and command substitution.
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f$`]")
# These run whatever follows them; a prefix would approve arbitrary programs.
_WRAPPERS = frozenset(
    {
        ".",
        "builtin",
        "caffeinate",
        "chroot",
        "command",
        "doas",
        "env",
        "eval",
        "exec",
        "flock",
        "ionice",
        "nice",
        "nocorrect",
        "noglob",
        "nohup",
        "open",
        "osascript",
        "parallel",
        "script",
        "source",
        "stdbuf",
        "su",
        "sudo",
        "time",
        "timeout",
        "unbuffer",
        "watch",
        "xargs",
    }
)
_INTERPRETER = re.compile(r"(python|pypy|node|deno|bun|ruby|perl|php|bash|sh|zsh|fish)[0-9.]*")
_MAX_PREFIX_WORDS = 3
_MAX_WORD = 200


def command_words(command: str | None) -> list[str] | None:
    """Return the words of a simple command, or None if shell syntax is present."""
    if not command or len(command) > 4000 or _UNSAFE_TEXT.search(command):
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # The shell only starts comments at a word boundary; shlex would otherwise
    # hide everything after a mid-word '#', including a following operator.
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:
        return None
    # Quoted operator characters are literal, but stay conservative: a word made
    # only of operator characters requires exact approval.
    if not words or any(set(word) <= _OPERATORS for word in words):
        return None
    return words


def parse_prefix(text: str) -> list[str] | None:
    """Parse an owner-configured prefix such as 'git status' or 'uv run pytest'."""
    words = command_words(text)
    if words is None or len(words) > _MAX_PREFIX_WORDS + 2:
        return None
    if PurePosixPath(words[0]).name in _WRAPPERS:
        return None
    return words


def command_class(command: str | None) -> list[str] | None:
    """Leading words an owner may approve for similar commands in one task.

    The class keeps the program and up to two subcommand words (stopping at an
    option or path). An interpreter is bound to its module or script, and
    wrappers that execute their arguments are never offered.
    """
    words = command_words(command)
    if words is None or PurePosixPath(words[0]).name in _WRAPPERS:
        return None
    prefix = [words[0]]
    index = 1
    while True:
        if _INTERPRETER.fullmatch(PurePosixPath(prefix[-1]).name):
            following = words[index : index + 2]
            if following[:1] == ["-m"] and len(following) == 2:
                prefix += following
            elif following and not following[0].startswith("-"):
                prefix.append(following[0])
            else:
                # Inline code (-c/-e) or a bare interpreter: exact approval only.
                return None
            break
        if index >= len(words) or len(prefix) >= _MAX_PREFIX_WORDS:
            break
        word = words[index]
        if word.startswith("-") or any(c in word for c in "/.=:~*?[{ \t"):
            break
        if PurePosixPath(word).name in _WRAPPERS:
            return None
        prefix.append(word)
        index += 1
    if any(len(word) > _MAX_WORD for word in prefix):
        return None
    return prefix


def matches_prefix(command: str | None, prefixes: list[list[str]]) -> bool:
    words = command_words(command)
    if words is None:
        return False
    return any(prefix and words[: len(prefix)] == prefix for prefix in prefixes)


# Programs that only read files or report system facts. Each maps to options
# that make it write, follow forever or run another program. A read-only class
# applies only inside Seatbelt, which already confines writes to the task.
# git is deliberately absent: repository config such as core.fsmonitor or a
# diff driver runs arbitrary programs, and Rocky can write that config itself.
_READ_ONLY_PROGRAMS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # program: (denied short option letters, denied long option names)
    "basename": (frozenset(), frozenset()),
    "cat": (frozenset(), frozenset()),
    "cmp": (frozenset(), frozenset()),
    "cut": (frozenset(), frozenset()),
    "df": (frozenset(), frozenset()),
    "diff": (frozenset(), frozenset()),
    "dirname": (frozenset(), frozenset()),
    "du": (frozenset(), frozenset()),
    "file": (frozenset("C"), frozenset({"compile"})),
    "grep": (frozenset(), frozenset()),
    "egrep": (frozenset(), frozenset()),
    "fgrep": (frozenset(), frozenset()),
    "head": (frozenset(), frozenset()),
    "hexdump": (frozenset(), frozenset()),
    "jq": (frozenset(), frozenset()),
    "ls": (frozenset(), frozenset()),
    "md5": (frozenset(), frozenset()),
    "md5sum": (frozenset(), frozenset()),
    "nl": (frozenset(), frozenset()),
    "od": (frozenset(), frozenset()),
    "pwd": (frozenset(), frozenset()),
    "readlink": (frozenset(), frozenset()),
    "realpath": (frozenset(), frozenset()),
    "rg": (frozenset(), frozenset({"pre", "pre-glob"})),
    "sha1sum": (frozenset(), frozenset()),
    "sha256sum": (frozenset(), frozenset()),
    "shasum": (frozenset(), frozenset()),
    "sort": (frozenset("oT"), frozenset({"output", "compress-program", "temporary-directory"})),
    "stat": (frozenset(), frozenset()),
    "strings": (frozenset(), frozenset()),
    "tail": (frozenset("fF"), frozenset({"follow", "retry"})),
    # -R reruns tree and writes 00Tree.html files.
    "tree": (frozenset("oR"), frozenset()),
    "uname": (frozenset(), frozenset()),
    "wc": (frozenset(), frozenset()),
    "which": (frozenset(), frozenset()),
    "whoami": (frozenset(), frozenset()),
}
# find spells its actions as single-dash words, never as option clusters.
_FIND_DENIED = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"}
)
READ_ONLY_PROGRAMS = tuple(sorted({*_READ_ONLY_PROGRAMS, "find"}))
_GLOB = frozenset("*?[]{}")


def _unquoted_expansion(command: str) -> bool:
    """Whether the shell could expand a word into other words or option names.

    An unquoted glob can expand to a file named like a denied option, and a
    leading ~ or = (zsh) expands to a path. Quoted text is literal.
    """
    lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    for token in tokens:
        if token[:1] in {"~", "="}:
            return True
        quote = ""
        escaped = False
        for character in token:
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif quote:
                if character == quote:
                    quote = ""
            elif character in {"'", '"'}:
                quote = character
            elif character in _GLOB:
                return True
    return False


def read_only_command(command: str | None) -> bool:
    """Whether a simple command only reads, so Seatbelt may run it unreviewed."""
    words = command_words(command)
    if words is None or words[0] not in READ_ONLY_PROGRAMS or _unquoted_expansion(command):
        return False
    if words[0] == "find":
        return not any(word in _FIND_DENIED for word in words[1:])
    short, long = _READ_ONLY_PROGRAMS[words[0]]
    for word in words[1:]:
        if word == "--":
            break
        if word.startswith("--"):
            name = word[2:].partition("=")[0]
            # GNU tools accept any unambiguous abbreviation of a long option.
            if name and any(option.startswith(name) for option in long):
                return False
        elif word.startswith("-") and short.intersection(word[1:]):
            return False
    return True
