"""Bounded, descriptor-relative file discovery without a shell or link traversal."""

import fnmatch
import json
import os
import stat
import time
from collections.abc import Callable
from functools import cache

MAX_ENTRIES = 10_000
MAX_FILE_BYTES = 1_000_000
MAX_SCAN_BYTES = 20_000_000
MAX_SECONDS = 5.0
MAX_DEPTH = 32
MAX_OUTPUT = 16_000
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".rocky",
        ".rocky-sandbox",
    }
)


class SearchOptionsError(ValueError):
    """Safe argument feedback, never interpolating supplied content."""


def parse_search_options(tool: str, content: str | None) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        options = json.loads(content, object_pairs_hook=unique) if content is not None else {}
    except (ValueError, TypeError, RecursionError):
        raise SearchOptionsError(
            "Search content must be a JSON object without duplicate fields."
        ) from None
    allowed = {"glob", "limit", "offset", "include_hidden"}
    if tool == "search_files":
        allowed |= {"query", "case_sensitive", "context_lines"}
    if not isinstance(options, dict) or set(options) - allowed:
        raise SearchOptionsError(
            "Unknown search option; use glob, limit, offset, include_hidden, and for search_files: query, case_sensitive, context_lines."
        )
    glob = options.setdefault("glob", "**/*")
    if (
        not isinstance(glob, str)
        or not 1 <= len(glob) <= 240
        or glob.startswith("/")
        or "\\" in glob
        or any(ord(c) < 32 or ord(c) == 127 for c in glob)
        or any(part in {"", ".", ".."} for part in glob.split("/"))
    ):
        raise SearchOptionsError(
            "glob must be a relative file pattern up to 240 characters, such as **/*.py, without traversal or empty segments."
        )
    for name, default, low, high in [("limit", 50, 1, 200), ("offset", 0, 0, 10_000)]:
        value = options.setdefault(name, default)
        if type(value) is not int or not low <= value <= high:
            raise SearchOptionsError(f"{name} must be an integer from {low} to {high}.")
    for name in ["include_hidden", *(["case_sensitive"] if tool == "search_files" else [])]:
        value = options.setdefault(name, name == "case_sensitive")
        if type(value) is not bool:
            raise SearchOptionsError(f"{name} must be true or false.")
    if tool == "search_files":
        query = options.get("query")
        if (
            not isinstance(query, str)
            or not 1 <= len(query) <= 500
            or "\x00" in query
            or "\n" in query
            or "\r" in query
        ):
            raise SearchOptionsError(
                "search_files requires a literal, single-line query of 1 to 500 characters; regular expressions are not supported."
            )
        context = options.setdefault("context_lines", 0)
        if type(context) is not int or not 0 <= context <= 3:
            raise SearchOptionsError("context_lines must be an integer from 0 to 3.")
    return options


def matches_glob(path: str, pattern: str) -> bool:
    """Case-sensitive shell globs; ** spans directories, * stays in one segment."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    names, patterns = path.split("/"), pattern.split("/")

    @cache
    def match(i: int, j: int) -> bool:
        if j == len(patterns):
            return i == len(names)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(names) and match(i + 1, j))
        return i < len(names) and fnmatch.fnmatchcase(names[i], patterns[j]) and match(i + 1, j + 1)

    return match(0, 0)


def search_directory(
    directory_fd: int,
    tool: str,
    options: dict,
    *,
    root: str,
    visible: Callable[[str], bool] = lambda _path: True,
) -> dict:
    """Walk only regular, single-link files; never follow directory symlinks.

    An offset pages a deterministic traversal of an unchanged tree. Scan limits
    are explicit: callers must narrow the root/glob rather than assume absence.
    """
    result = {
        "root": root,
        "matches": [],
        "truncated": False,
        "complete": True,
        "next_offset": None,
        "stop_reason": "",
        "scanned_files": 0,
        "visited_entries": 0,
        "scanned_bytes": 0,
        "skipped": dict.fromkeys(
            [
                "excluded",
                "links",
                "special",
                "protected",
                "unreadable",
                "binary",
                "oversized",
                "depth",
            ],
            0,
        ),
    }
    deadline = time.monotonic() + MAX_SECONDS
    matched = 0

    def stop(reason: str) -> None:
        result["truncated"] = True
        result["stop_reason"] = reason

    def budget() -> bool:
        if time.monotonic() >= deadline:
            stop("time_limit")
        return not result["truncated"]

    def append(item: dict) -> None:
        nonlocal matched
        matched += 1
        if matched <= options["offset"]:
            return
        if len(result["matches"]) >= options["limit"]:
            stop("result_limit")
            return
        result["matches"].append(item)
        # Reserve room for final status metadata; never clip serialized JSON.
        if len(json.dumps(result, ensure_ascii=False)) > MAX_OUTPUT - 500:
            result["matches"].pop()
            stop("output_limit")

    def visit(descriptor: int, base: str, depth: int) -> None:
        if not budget():
            return
        remaining = MAX_ENTRIES - result["visited_entries"]
        names = []
        overflow = False
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= remaining:
                    overflow = True
                    break
                names.append(entry.name)
                if not budget():
                    return
        for name in sorted(names):
            if not budget():
                return
            if result["visited_entries"] >= MAX_ENTRIES:
                stop("entry_limit")
                return
            result["visited_entries"] += 1
            relative = f"{base}/{name}" if base else name
            if name.startswith(".") and not options["include_hidden"]:
                result["skipped"]["excluded"] += 1
                continue
            if not visible(relative):
                result["skipped"]["protected"] += 1
                continue
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or (
                    stat.S_ISREG(info.st_mode) and info.st_nlink != 1
                ):
                    result["skipped"]["links"] += 1
                elif stat.S_ISDIR(info.st_mode):
                    if name in EXCLUDED_DIRECTORIES:
                        result["skipped"]["excluded"] += 1
                        continue
                    if depth >= MAX_DEPTH:
                        result["skipped"]["depth"] += 1
                        continue
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        visit(child, relative, depth + 1)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode):
                    result["skipped"]["special"] += 1
                elif matches_glob(relative, options["glob"]):
                    if tool == "find_files":
                        result["scanned_files"] += 1
                        append({"path": relative, "size_bytes": info.st_size})
                    else:
                        search_file(descriptor, name, relative)
            except OSError:
                result["skipped"]["unreadable"] += 1
        if overflow and not result["truncated"]:
            stop("entry_limit")

    def search_file(descriptor: int, name: str, relative: str) -> None:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                result["skipped"]["links"] += 1
                return
            if info.st_size > MAX_FILE_BYTES:
                result["skipped"]["oversized"] += 1
                return
            allowance = min(MAX_FILE_BYTES, MAX_SCAN_BYTES - result["scanned_bytes"])
            data = handle.read(allowance + 1)
        result["scanned_bytes"] += len(data)
        if len(data) > allowance:
            if allowance < MAX_FILE_BYTES:
                stop("byte_limit")
            else:
                result["skipped"]["oversized"] += 1
            return
        result["scanned_files"] += 1
        try:
            if b"\x00" in data:
                raise UnicodeError
            # read_file counts LF-terminated physical lines. str.splitlines()
            # also splits form feeds, NEL and Unicode paragraph separators,
            # which would make search locations unusable for targeted reads.
            parts = data.decode("utf-8").split("\n")
            lines = [part.removesuffix("\r") for part in parts[:-1]]
            if parts[-1]:
                lines.append(parts[-1])
        except UnicodeError:
            result["skipped"]["binary"] += 1
            return
        query = options["query"] if options["case_sensitive"] else options["query"].casefold()
        for i, line in enumerate(lines):
            if i % 128 == 0 and not budget():
                return
            if query not in (line if options["case_sensitive"] else line.casefold()):
                continue
            item = {
                "path": relative,
                "line": i + 1,
                "text": line[:400],
                "line_truncated": len(line) > 400,
            }
            if options["context_lines"]:
                distance = options["context_lines"]
                item["context"] = [
                    {"line": j + 1, "text": lines[j][:400], "line_truncated": len(lines[j]) > 400}
                    for j in range(max(0, i - distance), min(len(lines), i + distance + 1))
                    if j != i
                ]
            append(item)
            if result["truncated"]:
                return

    visit(directory_fd, "", 0)
    if result["stop_reason"] in {"result_limit", "output_limit"} and result["matches"]:
        next_offset = options["offset"] + len(result["matches"])
        if next_offset <= 10_000:
            result["next_offset"] = next_offset
        else:
            result["stop_reason"] = "offset_limit"
    result["complete"] = not result["truncated"] and not any(
        count for name, count in result["skipped"].items() if name != "excluded"
    )
    return result
