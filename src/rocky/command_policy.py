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
