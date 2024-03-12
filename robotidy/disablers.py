import functools
import re
from typing import Dict, List, Optional

from robot.api.parsing import Comment, CommentSection, ModelVisitor, Token

ALL_TRANSFORMERS = "all"


def skip_if_disabled(func):
    """
    Do not transform node if it's not within passed ``start_line`` and ``end_line`` or
    it does match any ``# robotidy: off`` disabler
    """

    @functools.wraps(func)
    def wrapper(self, node, *args, **kwargs):
        class_name = self.__class__.__name__
        if self.disablers.is_node_disabled(class_name, node):
            return node
        return func(self, node, *args, **kwargs)

    return wrapper


def get_section_name_from_header_type(node):
    header_type = node.header.type if node.header else "COMMENT HEADER"
    return {
        "SETTING HEADER": "settings",
        "VARIABLE HEADER": "variables",
        "TESTCASE HEADER": "testcases",
        "TASK HEADER": "tasks",
        "KEYWORD HEADER": "keywords",
        "COMMENT HEADER": "comments",
    }.get(header_type, "invalid")


def skip_section_if_disabled(func):
    """
    Does the same checks as ``skip_if_disabled`` and additionally checks
    if the section header does not contain disabler
    """

    @functools.wraps(func)
    def wrapper(self, node, *args, **kwargs):
        class_name = self.__class__.__name__
        if self.disablers.is_node_disabled(class_name, node):
            return node
        if self.disablers.is_header_disabled(class_name, node.lineno):
            return node
        if self.skip:
            section_name = get_section_name_from_header_type(node)
            if self.skip.section(section_name):
                return node
        return func(self, node, *args, **kwargs)

    return wrapper


def is_line_start(node):
    for token in node.tokens:
        if token.type == Token.SEPARATOR:
            continue
        return token.col_offset == 0
    return False


class DisablersInFile:
    def __init__(self, start_line: Optional[int], end_line: Optional[int], file_end: Optional[int] = None):
        self.start_line = start_line
        self.end_line = end_line
        self.file_end = file_end
        self.disablers = {ALL_TRANSFORMERS: DisabledLines(start_line, end_line, file_end)}

    def parse_global_disablers(self):
        self.disablers[ALL_TRANSFORMERS].parse_global_disablers()

    def sort_disablers(self):
        for disabled_lines in self.disablers.values():
            disabled_lines.sort_disablers()

    def add_disabler(self, transformer: str, start_line: int, end_line: int, file_level: bool = False):
        if transformer not in self.disablers:
            self.disablers[transformer] = DisabledLines(self.start_line, self.end_line, self.file_end)
        self.disablers[transformer].add_disabler(start_line, end_line)
        if file_level:
            self.disablers[transformer].disabled_whole = file_level

    def add_disabled_header(self, transformer: str, lineno):
        if transformer not in self.disablers:
            self.disablers[transformer] = DisabledLines(self.start_line, self.end_line, self.file_end)
        self.disablers[transformer].add_disabled_header(lineno)

    def is_disabled_in_file(self, transformer_name: str) -> bool:
        if self.disablers[ALL_TRANSFORMERS].disabled_whole:
            return True
        if transformer_name not in self.disablers:
            return False
        return self.disablers[transformer_name].disabled_whole

    def is_header_disabled(self, transformer_name: str, line) -> bool:
        if self.disablers[ALL_TRANSFORMERS].is_header_disabled(line):
            return True
        if transformer_name not in self.disablers:
            return False
        return self.disablers[transformer_name].is_header_disabled(line)

    def is_node_disabled(self, transformer_name: str, node, full_match=True) -> bool:
        if self.disablers[ALL_TRANSFORMERS].is_node_disabled(node, full_match):
            return True
        if transformer_name not in self.disablers:
            return False
        return self.disablers[transformer_name].is_node_disabled(node, full_match)


class DisabledLines:
    def __init__(self, start_line, end_line, file_end):
        self.start_line = start_line
        self.end_line = end_line
        self.file_end = file_end
        self.lines = []
        self.disabled_headers = set()
        self.disabled_whole = False

    def add_disabler(self, start_line, end_line):
        self.lines.append((start_line, end_line))

    def add_disabled_header(self, lineno):
        self.disabled_headers.add(lineno)

    def parse_global_disablers(self):
        if not self.start_line:
            return
        end_line = self.end_line if self.end_line else self.start_line
        if self.start_line > 1:
            self.add_disabler(1, self.start_line - 1)
        if end_line < self.file_end:
            self.add_disabler(end_line + 1, self.file_end)

    def sort_disablers(self):
        self.lines = sorted(self.lines, key=lambda x: x[0])

    def is_header_disabled(self, line):
        return line in self.disabled_headers

    def is_node_disabled(self, node, full_match=True):
        if not node or not self.lines:
            return False
        end_lineno = max(node.lineno, node.end_lineno)  # workaround for transformers setting -1 as end_lineno
        if full_match:
            for start_line, end_line in self.lines:
                # lines are sorted on start_line, so we can return on first match
                if end_line >= end_lineno:
                    return start_line <= node.lineno
        else:
            for start_line, end_line in self.lines:
                if node.lineno <= end_line and end_lineno >= start_line:
                    return True
        return False


class RegisterDisablers(ModelVisitor):
    def __init__(self, start_line, end_line):
        self.start_line = start_line
        self.end_line = end_line
        self.disablers = DisablersInFile(start_line, end_line)
        self.disabler_pattern = re.compile(r"\s*#\s?robotidy:\s?(?P<disabler>on|off) ?=?(?P<transformers>[\w,\s]*)")
        self.disablers_in_scope: List[Dict[str, int]] = []
        self.file_level_disablers = False

    def is_disabled_in_file(self, transformer_name: str = ALL_TRANSFORMERS):
        return self.disablers.is_disabled_in_file(transformer_name)

    def get_disabler(self, comment):
        if not comment.value:
            return None
        return self.disabler_pattern.match(comment.value)

    def close_disabler(self, end_line):
        disabler = self.disablers_in_scope.pop()
        for transformer_name, start_line in disabler.items():
            if not start_line:
                continue
            self.disablers.add_disabler(transformer_name, start_line, end_line, self.file_level_disablers)

    def visit_File(self, node):  # noqa
        self.file_level_disablers = False
        self.disablers = DisablersInFile(self.start_line, self.end_line, node.end_lineno)
        self.disablers.parse_global_disablers()
        self.stack = []
        for index, section in enumerate(node.sections):
            self.file_level_disablers = index == 0 and isinstance(section, CommentSection)
            self.visit_Section(section)
        self.disablers.sort_disablers()

    @staticmethod
    def get_disabler_transformers(match) -> List[str]:
        if not match.group("transformers") or "=" not in match.group(0):  # robotidy: off or robotidy: off comment
            return [ALL_TRANSFORMERS]
        # robotidy: off=Transformer1, Transformer2
        return [transformer.strip() for transformer in match.group("transformers").split(",") if transformer.strip()]

    def visit_SectionHeader(self, node):  # noqa
        for comment in node.get_tokens(Token.COMMENT):
            disabler = self.get_disabler(comment)
            if not disabler or disabler.group("disabler") != "off":
                continue
            transformers = self.get_disabler_transformers(disabler)
            for transformer in transformers:
                self.disablers.add_disabled_header(transformer, node.lineno)
            break
        return self.generic_visit(node)

    def visit_TestCase(self, node):  # noqa
        self.disablers_in_scope.append({ALL_TRANSFORMERS: 0})
        self.generic_visit(node)
        self.close_disabler(node.end_lineno)

    def visit_Try(self, node):  # noqa
        self.generic_visit(node.header)
        self.disablers_in_scope.append({ALL_TRANSFORMERS: 0})
        for statement in node.body:
            self.visit(statement)
        self.close_disabler(node.end_lineno)
        tail = node
        while tail.next:
            self.generic_visit(tail.header)
            self.disablers_in_scope.append({ALL_TRANSFORMERS: 0})
            for statement in tail.body:
                self.visit(statement)
            end_line = tail.next.lineno - 1 if tail.next else tail.end_lineno
            self.close_disabler(end_line=end_line)
            tail = tail.next

    visit_Keyword = visit_Section = visit_For = visit_ForLoop = visit_If = visit_While = visit_TestCase

    def visit_Statement(self, node):  # noqa
        if isinstance(node, Comment):
            comment = node.get_token(Token.COMMENT)
            disabler = self.get_disabler(comment)
            if not disabler:
                return
            transformers = self.get_disabler_transformers(disabler)
            index = 0 if is_line_start(node) else -1
            disabler_start = disabler.group("disabler") == "on"
            for transformer in transformers:
                if disabler_start:
                    start_line = self.disablers_in_scope[index].get(transformer)
                    if not start_line:  # no disabler open
                        continue
                    self.disablers.add_disabler(transformer, start_line, node.lineno)
                    self.disablers_in_scope[index][transformer] = 0
                else:
                    if not self.disablers_in_scope[index].get(transformer):
                        self.disablers_in_scope[index][transformer] = node.lineno
        else:
            # inline disabler
            for comment in node.get_tokens(Token.COMMENT):
                disabler = self.get_disabler(comment)
                if not disabler:
                    continue
                transformers = self.get_disabler_transformers(disabler)
                if disabler.group("disabler") == "off":
                    for transformer in transformers:
                        self.disablers.add_disabler(transformer, node.lineno, node.end_lineno)
