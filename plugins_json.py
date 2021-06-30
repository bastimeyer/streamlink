#!/usr/bin/env python3
import ast
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple, Tuple


class Matcher(NamedTuple):
    pattern: str
    flags: int
    priority: int


class ParseRegExpFlagsOrPriorityExpression(ast.NodeVisitor):
    """
    Parse `attr_or_const_or_name | attr_or_const_or_name`
    """

    def generic_visit(self, node: ast.AST):
        raise ValueError("Unsupported node type")

    def visit_BinOp(self, node: ast.BinOp) -> int:
        if not isinstance(node.op, ast.BitOr):
            raise ValueError("Unsupported binary operator")
        return self.visit(node.left) | self.visit(node.right)

    def visit_Constant(self, node: ast.Constant) -> int:
        if not str(node.value).isdigit():
            raise ValueError("Unsupported constant type")
        return int(node.value)


class ParseRegExpFlags(ParseRegExpFlagsOrPriorityExpression):
    TEMPLATE = 1 << 0
    IGNORECASE = 1 << 1
    LOCALE = 1 << 2
    MULTILINE = 1 << 3
    DOTALL = 1 << 4
    UNICODE = 1 << 5
    VERBOSE = 1 << 6
    DEBUG = 1 << 7
    ASCII = 1 << 8

    def visit_Attribute(self, node: ast.Attribute) -> int:
        if not isinstance(node.value, ast.Name) or node.value.id != "re" or not hasattr(self, node.attr):
            raise ValueError("Unsupported attribute")
        return getattr(self, node.attr)


class ParseMatcherArgPriority(ParseRegExpFlagsOrPriorityExpression):
    """
    Parse priority argument of `@pluginmatcher()`
    """

    NO_PRIORITY = 0
    LOW_PRIORITY = 10
    NORMAL_PRIORITY = 20
    HIGH_PRIORITY = 30

    def visit_Name(self, node: ast.Name) -> int:
        if not hasattr(self, node.id):
            raise ValueError("Unsupported name")
        return getattr(self, node.id)


class ParseMatcherArgPattern(ast.NodeVisitor):
    """
    Parse pattern argument of `@pluginmatcher()`, which must be a `re.compile()` call
    and turn verbose patterns into non-verbose patterns
    """

    _re_whitespace = re.compile(r"\s")

    def generic_visit(self, node: ast.AST):
        raise ValueError("Invalid pluginmatcher pattern: unknown AST node")

    def visit_Call(self, node: ast.Call) -> Tuple[str, int]:
        if (
            not isinstance(node.func, ast.Attribute)
            or node.func.attr != "compile"
            or not isinstance(node.func.value, ast.Name)
            or node.func.value.id != "re"
        ):
            raise ValueError("Invalid pluginmatcher pattern: not a compiled regex")

        pattern, flags = None, 0
        for idx, arg in enumerate(node.args or []):
            if idx == 0:
                if not isinstance(arg, ast.Constant) or type(arg.value) is not str:
                    raise ValueError("Invalid pluginmatcher pattern: invalid pattern type")
                pattern = arg.value
            elif idx == 1:
                flags = ParseRegExpFlags().visit(arg)
        for keyword in node.keywords:
            if keyword.arg == "pattern":
                if not isinstance(keyword.value, ast.Constant) or type(keyword.value.value) is not str:
                    raise ValueError("Invalid pluginmatcher pattern: invalid pattern type")
                pattern = keyword.value.value
            elif keyword.arg == "flags":
                flags = ParseRegExpFlags().visit(keyword.value)

        if not pattern:
            raise ValueError("Invalid pluginmatcher pattern: missing pattern")

        if (flags & ParseRegExpFlags.VERBOSE) == ParseRegExpFlags.VERBOSE:
            pattern = self._re_whitespace.sub("", pattern)
            flags = flags & ~ParseRegExpFlags.VERBOSE

        return pattern, flags


class FindMatchers(ast.NodeVisitor):
    matchers = None
    name_plugin = None
    exports = False

    def generic_visit(self, node):
        pass

    def visit_Module(self, node: ast.Module):
        for body in node.body:
            self.visit(body)

    def visit_ClassDef(self, node: ast.ClassDef):
        for base in node.bases:
            if not isinstance(base, ast.Name):
                continue
            if base.id == "Plugin":
                break
        else:
            return

        self.matchers = []
        self.name_plugin = node.name
        for decorator in node.decorator_list:
            if (
                not isinstance(decorator, ast.Call)
                or not isinstance(decorator.func, ast.Name)
                or decorator.func.id != "pluginmatcher"
                or (len(decorator.args) == 0 and len(decorator.keywords) == 0)
            ):
                continue

            pattern, flags, priority = None, 0, ParseMatcherArgPriority.NORMAL_PRIORITY
            for idx, arg in enumerate(decorator.args or []):
                if idx == 0:
                    pattern, flags = ParseMatcherArgPattern().visit(arg)
                elif idx == 1:
                    priority = ParseMatcherArgPriority().visit(arg)
            for keyword in decorator.keywords:
                if keyword.arg == "pattern":
                    pattern, flags = ParseMatcherArgPattern().visit(keyword.value)
                elif keyword.arg == "priority":
                    priority = ParseMatcherArgPriority().visit(keyword.value)

            self.matchers.append(Matcher(pattern, flags, priority))

    def visit_Assign(self, node: ast.Assign):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == self.name_plugin
            and self.name_plugin is not None
        ):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__plugin__":
                    self.exports = True


def find_plugins(pluginsdir: str, glob: str):
    data = {}
    for pluginpath in Path(pluginsdir).glob(glob):
        name = pluginpath.name
        try:
            source = pluginpath.read_text()
        except OSError:
            sys.stderr.write(f"ERR: Could not read file {pluginpath}")
            continue

        try:
            tree = ast.parse(source, str(pluginpath))
        except (SyntaxError, ValueError):
            sys.stderr.write(f"ERR: Failed to load plugin {name} from {pluginsdir}\n")
            continue

        res = FindMatchers()
        res.visit(tree)

        if not res.matchers or not res.exports:
            continue

        data[name] = [
            {
                "pattern": matcher.pattern,
                "flags": matcher.flags,
                "priority": matcher.priority
            }
            for matcher in res.matchers
            if matcher.pattern is not None
        ]

    sys.stdout.write(json.dumps(data))


if __name__ == "__main__":
    find_plugins("./src/streamlink/plugins", "*[!_].py")
