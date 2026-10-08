"""把一行 shell 命令切成若干条 simple command，并定位每条在本机执行的程序。

本模块用于区分下面两种命令：

    rm -rf build/                 # 删除本机文件
    adb shell rm -rf /data/local  # 删除设备文件，rm 是 adb 的参数

正则匹配 \\brm\\b 两条都会命中。区分二者需要先做词法切分，再判断 rm 位于命令词
还是参数位置，只有命令词代表本机执行。

`sh -c '...'` 的参数会被递归展开，该字符串在本机执行；adb、ssh、docker exec
一类把参数送往别处执行的程序不展开。
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import NamedTuple


class Command(NamedTuple):
    """一条 simple command。

    word     命令词原文（可能带路径，如 /bin/rm）
    args     命令词后面的参数
    prefixes 被跳过的前缀词，如 sudo、xargs。sudo 会替换 PATH，调用方需据此判断。
    """

    word: str
    args: list[str]
    prefixes: list[str]

    @property
    def name(self) -> str:
        return PurePosixPath(self.word).name

    @property
    def is_path_qualified(self) -> bool:
        """命令词写成了路径形式（/bin/rm、./rm），会绕过 PATH 里的 shim。"""
        return "/" in self.word

    @property
    def escalates(self) -> bool:
        """经由 sudo 一类提权执行，PATH 会被换成 secure_path。"""
        return bool({"sudo", "doas", "pkexec", "run0"} & set(self.prefixes))

    @property
    def shell(self) -> str:
        """哪种 shell 语义。PowerShell 命令里嵌的 bash -c 会同时产出两种，
        删除策略不同（这边有 PATH 替身兜底，那边没有），必须能分辨。"""
        return "posix"

# 命令之间的分隔符。shlex(punctuation_chars=True) 会把连续标点聚成一个 token，
# 所以 && || |& ;; 都是单个词。
SEPARATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "{", "}"})

# 重定向算符，例如 > >> 2> 2>&1 <。算符和它后面的目标词都不是命令词。
REDIRECT = re.compile(r"^\d*(?:>>|>\||>&|>|<<<|<<|<&|<)\d*$")

# VAR=value 形式的前置赋值。
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 这些词本身不是"被执行的程序"，真正的程序在它们后面。
PREFIX_WORDS = frozenset({
    "sudo", "doas", "pkexec", "env", "command", "builtin", "exec",
    "nohup", "setsid", "time", "timeout", "stdbuf", "nice", "ionice",
    "xargs", "unbuffer", "watch", "proxychains", "proxychains4",
})

# 前缀词中自带一个参数的选项。跳过时须连同参数一并跳过，否则参数会被误判为
# 命令词，例如 sudo -u spencer rm 中的 spencer。
PREFIX_OPTS_WITH_ARG = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-r", "-t", "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "xargs": {"-n", "-I", "-i", "-L", "-P", "-a", "-d", "-E", "-s",
              "--max-args", "--replace", "--max-procs", "--arg-file",
              "--delimiter", "--max-lines", "--max-chars"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "watch": {"-n", "--interval"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
}

# 前缀词后面还要跳过的位置参数个数（timeout 5 cmd 里的 5、watch 的间隔）。
PREFIX_POSITIONAL = {"timeout": 1}

# 会将某个参数作为 shell 脚本在本机执行的程序，需递归解析该参数。
SHELL_WORDS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "busybox"})


class ParseError(Exception):
    """命令无法切分，通常是引号不闭合。调用方应按最保守的方式处理。"""


def tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:  # No closing quotation / No escaped character
        raise ParseError(str(exc)) from exc


def _split_lines(command: str) -> list[str]:
    """按行切分，跨行的引号重新合并。

    shlex 将换行视为普通空白，`ls\\nrm x` 会被并成一条命令，rm 退化为 ls 的参数。
    因此逐行处理；某行无法切分说明引号尚未闭合，与后续行合并后重试。
    """
    chunks: list[str] = []
    buf = ""
    for line in command.splitlines():
        buf = f"{buf}\n{line}" if buf else line
        try:
            tokenize(buf)
        except ParseError:
            continue
        chunks.append(buf)
        buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def _skip_prefix(words: list[str], i: int, prefix: str) -> int:
    """跳过前缀词自己的选项和位置参数，返回下一个候选命令词的下标。"""
    opts_with_arg = PREFIX_OPTS_WITH_ARG.get(prefix, set())
    positional = PREFIX_POSITIONAL.get(prefix, 0)
    while i < len(words):
        word = words[i]
        if word == "--":
            return i + 1
        if word.startswith("-") and word != "-":
            i += 1
            # --opt=value 已含参数，无需再跳过一个词
            if "=" not in word and word in opts_with_arg:
                i += 1
            continue
        if ASSIGNMENT.match(word) and prefix == "env":
            i += 1
            continue
        if positional:
            positional -= 1
            i += 1
            continue
        return i
    return i


def command_word(words: list[str]) -> Command | None:
    """从一条 simple command 的词表里取出命令词、参数和被跳过的前缀词。"""
    prefixes: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word):
            i += 1
            continue
        if REDIRECT.match(word):
            i += 2  # 算符 + 目标
            continue
        if word in SEPARATORS:
            i += 1
            continue
        name = PurePosixPath(word).name
        if name in PREFIX_WORDS:
            prefixes.append(name)
            i = _skip_prefix(words, i + 1, name)
            continue
        args = [w for w in words[i + 1:] if not REDIRECT.match(w)]
        return Command(word, args, prefixes)
    return None


def simple_commands(command: str) -> list[list[str]]:
    """按分隔符把命令切成若干条 simple command 的词表。"""
    result: list[list[str]] = []
    for chunk in _split_lines(command):
        current: list[str] = []
        for token in tokenize(chunk):
            if token in SEPARATORS:
                if current:
                    result.append(current)
                current = []
            else:
                current.append(token)
        if current:
            result.append(current)
    return result


def executed_commands(command: str, _depth: int = 0) -> list[Command]:
    """列出这行命令在本机实际执行的所有命令。

    `sh -c '...'`、`bash -c '...'` 里的脚本会被递归展开。
    """
    found: list[Command] = []
    for words in simple_commands(command):
        cmd = command_word(words)
        if cmd is None:
            continue
        found.append(cmd)
        if _depth < 3 and cmd.name in SHELL_WORDS:
            script = _shell_c_argument(cmd.args)
            if script:
                found.extend(executed_commands(script, _depth + 1))
    return found


def _shell_c_argument(args: list[str]) -> str | None:
    """取出 `sh -c <脚本>` 里的脚本部分。busybox sh -c 也算。"""
    for idx, arg in enumerate(args):
        if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
            if idx + 1 < len(args):
                return args[idx + 1]
            return None
    return None
