"""把一行 PowerShell 命令切成若干条 simple command，并定位每条在本机执行的程序。

与 shellcmds 同接口，但另起一份而不是在其中加分支：两种 shell 的词法几乎没有重合，
共用一个 lexer 只会让两边都不对。

- 转义符是反引号。反斜杠是路径分隔符，当转义会把 `C:\ntfs` 读成换行。
- 单引号内 '' 表示一个引号；双引号内反引号转义，且 $(...) 会在本机执行。
- 命令名大小写不敏感，`RM -Recurse x` 与 `rm -Recurse x` 等价。
- rm、del、ri、rd、erase 都是 Remove-Item 的内建别名。PowerShell 的名字解析顺序是
  别名 → 函数 → cmdlet → 外部命令，PATH 替身排在最后，永远轮不到它。这是
  PowerShell 侧只能拒绝删除、无法像 Bash 侧那样转投回收站的根本原因。
- & 和 . 是调用运算符，真正的程序在它后面。
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import NamedTuple

from shellcmds import ParseError
from shellcmds import executed_commands as posix_executed_commands


class Command(NamedTuple):
    """一条 simple command。字段含义与 shellcmds.Command 一致。"""

    word: str
    args: list[str]
    prefixes: list[str]

    @property
    def name(self) -> str:
        """规范化后的命令名：去路径、转小写、解析内建别名。"""
        return ALIASES.get(_basename(self.word), _basename(self.word))

    @property
    def is_path_qualified(self) -> bool:
        return "/" in self.word or "\\" in self.word

    @property
    def escalates(self) -> bool:
        return bool({"sudo", "gsudo"} & set(self.prefixes))

    @property
    def shell(self) -> str:
        return "powershell"


def _basename(word: str) -> str:
    """去路径、转小写，并去掉 Windows 的可执行扩展名。

    npm 在 PowerShell 里实际解析到 npm.cmd，写全名和写 npm 是同一件事，
    不归一化会让 `npm.cmd i -g x` 从装包门控下漏过去。
    """
    name = re.split(r"[/\\]", word)[-1].lower()
    return re.sub(r"\.(exe|cmd|bat|ps1|com)$", "", name)


# 只收录门控关心的别名。补全整张表没有意义，PowerShell 的别名可以由用户随时增删，
# 真正的权威是运行时的 Get-Alias，而 hook 不应该为此付一次 pwsh 启动的代价。
ALIASES = {
    "rm": "remove-item",
    "rmdir": "remove-item",
    "ri": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "rd": "remove-item",
    "iex": "invoke-expression",
    "gcm": "get-command",
    "sls": "select-string",
}

SEPARATORS = frozenset({";", "|", "||", "&&", "&", "(", ")", "{", "}", "$(", "@(", "@{", "\n"})

# 词的边界。& 与 | 既是分隔符也可能紧贴前一个词，故一并断开。
WORD_BREAK = frozenset(" \t\r\n;|&(){}")

REDIRECT = re.compile(r"^\d*(?:>>|>&|>|<)\d*$")

# 这些词本身不是被执行的程序。& 是调用运算符（& $exe args），. 是点源（. .\x.ps1）。
PREFIX_WORDS = frozenset({"&", ".", "sudo", "gsudo"})

# 会把某个参数当作 PowerShell 脚本在本机执行的程序。
PWSH_WORDS = frozenset({"pwsh", "pwsh.exe", "powershell", "powershell.exe"})

# 会把某个参数当作 POSIX shell 脚本在本机执行的程序，交回 shellcmds 解析。
POSIX_SHELL_WORDS = frozenset({"sh", "bash", "zsh", "dash", "sh.exe", "bash.exe"})


def _read_word(text: str, i: int) -> tuple[str, list[str], int]:
    """读一个词。词可以由多段拼成，如 C:\\p"a b"\\q 是一个词。

    返回 (词, 词内双引号里的 $(...) 子表达式, 新下标)。
    """
    out: list[str] = []
    subexpressions: list[str] = []
    n = len(text)

    while i < n:
        ch = text[i]
        if ch in WORD_BREAK:
            break

        if ch == "`":
            if i + 1 >= n:
                raise ParseError("行尾的反引号后没有字符")
            out.append(text[i + 1])
            i += 2
            continue

        if ch == "'":
            buf, i = _read_single_quoted(text, i)
            out.append(buf)
            continue

        if ch == '"':
            buf, i = _read_double_quoted(text, i)
            subexpressions.extend(_subexpressions(buf))
            out.append(buf)
            continue

        out.append(ch)
        i += 1

    return "".join(out), subexpressions, i


def _read_single_quoted(text: str, i: int) -> tuple[str, int]:
    buf: list[str] = []
    j = i + 1
    while True:
        if j >= len(text):
            raise ParseError("单引号未闭合")
        if text[j] == "'":
            if j + 1 < len(text) and text[j + 1] == "'":
                buf.append("'")
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(text[j])
        j += 1


def _read_double_quoted(text: str, i: int) -> tuple[str, int]:
    buf: list[str] = []
    j = i + 1
    while True:
        if j >= len(text):
            raise ParseError("双引号未闭合")
        ch = text[j]
        if ch == "`" and j + 1 < len(text):
            buf.append(text[j + 1])
            j += 2
            continue
        if ch == '"':
            if j + 1 < len(text) and text[j + 1] == '"':
                buf.append('"')
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(ch)
        j += 1


def _subexpressions(text: str) -> list[str]:
    """取出字符串里的 $(...) 内容。双引号内的子表达式在本机执行，需要单独解析。"""
    found: list[str] = []
    i = 0
    while True:
        start = text.find("$(", i)
        if start < 0:
            return found
        depth, j = 1, start + 2
        while j < len(text) and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        if depth:  # 不闭合，剩下的交给调用方按最保守方式处理
            return found
        found.append(text[start + 2:j - 1])
        i = j


def tokenize(command: str) -> tuple[list[str], list[str]]:
    """返回 (词表, 双引号内的子表达式)。"""
    tokens: list[str] = []
    subexpressions: list[str] = []
    i, n = 0, len(command)

    while i < n:
        ch = command[i]

        if ch in " \t\r":
            i += 1
        elif ch == "\n":
            tokens.append("\n")
            i += 1
        elif ch == "#":
            while i < n and command[i] != "\n":
                i += 1
        elif command.startswith("||", i) or command.startswith("&&", i):
            tokens.append(command[i:i + 2])
            i += 2
        elif command.startswith("$(", i) or command.startswith("@(", i) or command.startswith("@{", i):
            tokens.append(command[i:i + 2])
            i += 2
        elif ch in ";|&(){}":
            tokens.append(ch)
            i += 1
        else:
            word, subs, i = _read_word(command, i)
            if word:
                tokens.append(word)
            else:
                i += 1  # 保底：不消费字符会死循环
            subexpressions.extend(subs)

    return tokens, subexpressions


def command_word(words: list[str]) -> Command | None:
    prefixes: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]

        if REDIRECT.match(word):
            i += 2  # 算符 + 目标
            continue
        # $x = ... 的左侧与 = 都不是命令词，右侧仍可能是命令（$x = Remove-Item y）。
        if word.startswith("$") or word == "=" or word in SEPARATORS:
            i += 1
            continue

        name = _basename(word)
        if name in PREFIX_WORDS or word in PREFIX_WORDS:
            prefixes.append(name)
            i += 1
            continue

        args = [w for w in words[i + 1:] if not REDIRECT.match(w)]
        return Command(word, args, prefixes)
    return None


def simple_commands(command: str) -> tuple[list[list[str]], list[str]]:
    tokens, subexpressions = tokenize(command)
    result: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result, subexpressions


def executed_commands(command: str, _depth: int = 0) -> list[Command]:
    """列出这行 PowerShell 命令在本机实际执行的所有命令。"""
    found: list[Command] = []
    groups, subexpressions = simple_commands(command)

    for words in groups:
        cmd = command_word(words)
        if cmd is None:
            continue
        found.append(cmd)
        if _depth >= 3:
            continue
        for nested in _nested_scripts(cmd):
            found.extend(executed_commands(nested, _depth + 1))
        for script in _posix_scripts(cmd):
            found.extend(posix_executed_commands(script))

    if _depth < 3:
        for expression in subexpressions:
            found.extend(executed_commands(expression, _depth + 1))

    return found


def _nested_scripts(cmd: Command) -> list[str]:
    """取出会被当作 PowerShell 代码执行的参数。"""
    name = _basename(cmd.word)

    if name == "invoke-expression" or ALIASES.get(name) == "invoke-expression":
        # 参数是变量时无从解析，那属于已知边界。
        return [a for a in cmd.args if not a.startswith("$")]

    if name not in PWSH_WORDS:
        return []

    scripts: list[str] = []
    for idx, arg in enumerate(cmd.args):
        if idx + 1 >= len(cmd.args):
            break
        flag = arg.lower()
        # PowerShell 接受参数名前缀，-c/-com/-command 都指向 -Command。
        if flag.startswith("-c") and "command".startswith(flag[1:]):
            scripts.append(cmd.args[idx + 1])
        elif flag.startswith("-e") and "encodedcommand".startswith(flag[1:]):
            decoded = _decode_encoded_command(cmd.args[idx + 1])
            if decoded:
                scripts.append(decoded)
    return scripts


def _decode_encoded_command(payload: str) -> str | None:
    """-EncodedCommand 的参数是 UTF-16LE 的 base64。不解码等于留一个明面上的绕过口。"""
    try:
        return base64.b64decode(payload, validate=True).decode("utf-16-le")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _posix_scripts(cmd: Command) -> list[str]:
    """取出 `bash -c '...'` 一类会以 POSIX shell 语义在本机执行的参数。"""
    if _basename(cmd.word) not in POSIX_SHELL_WORDS:
        return []
    for idx, arg in enumerate(cmd.args):
        if arg == "-c" and idx + 1 < len(cmd.args):
            return [cmd.args[idx + 1]]
    return []
