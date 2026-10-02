"""Statically visible selector context reads, never runtime evidence or source code."""

from __future__ import annotations

import ast
import dis
import inspect
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MetricInput(BaseModel):
    """A source-relative context path, or the identity of an opaque selector."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Literal["trace", "output", "input", "custom"]
    selector: str


def _callable_identity(selector: Callable[..., object]) -> str:
    if (
        inspect.isfunction(selector)
        or inspect.isbuiltin(selector)
        or inspect.ismethod(selector)
    ):
        return selector.__qualname__
    return f"{type(selector).__module__}.{type(selector).__qualname__}"


def _selector_node(selector: Callable[..., object]) -> ast.FunctionDef | ast.Lambda | None:
    # Callable objects and wrappers are not proof of their delegated implementation.
    if not inspect.isfunction(selector):
        return None
    try:
        lines, _ = inspect.findsource(selector)
    except (OSError, TypeError):
        return None
    tree = ast.parse("".join(lines))
    code = selector.__code__
    if code.co_name != "<lambda>":
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == code.co_name
            and min([node.lineno, *(item.lineno for item in node.decorator_list)])
            == code.co_firstlineno
        ]
    else:
        # Line numbers alone cannot distinguish adjacent lambdas. Bytecode positions
        # locate this callable's own expression, including multiple lambdas per line.
        positions = [
            instruction.positions
            for instruction in dis.get_instructions(selector)
            if instruction.opname not in {"RESUME", "COPY_FREE_VARS", "RETURN_VALUE"}
            and instruction.positions.lineno is not None
            and instruction.positions.end_lineno is not None
            and instruction.positions.col_offset is not None
            and instruction.positions.end_col_offset is not None
        ]
        if not positions:
            return None
        candidates = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Lambda) or node.lineno != code.co_firstlineno:
                continue
            body = node.body
            if body.end_lineno is None or body.end_col_offset is None:
                continue
            if all(
                (body.lineno, body.col_offset) <= (position.lineno, position.col_offset)
                and (position.end_lineno, position.end_col_offset)
                <= (body.end_lineno, body.end_col_offset)
                for position in positions
            ):
                candidates.append(node)
    return candidates[0] if len(candidates) == 1 else None


def _static_index(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return repr(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        return str(-node.operand.value)
    if isinstance(node, ast.Slice):
        parts = [
            "" if item is None else _static_index(item)
            for item in (node.lower, node.upper, node.step)
        ]
        if all(part is not None for part in parts):
            return ":".join(
                part
                for part in (parts if node.step is not None else parts[:2])
                if part is not None
            )
    return None


class _ContextReads(ast.NodeVisitor):
    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        self.reads: dict[tuple[str, str], MetricInput] = {}

    def _path(self, node: ast.AST) -> MetricInput | None:
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == self.parameter:
                if node.attr == "inputs":
                    return MetricInput(source="input", selector="")
                if node.attr == "output":
                    return MetricInput(source="output", selector="")
                if node.attr == "trace":
                    return MetricInput(source="trace", selector="")
            parent = self._path(node.value)
            if parent is not None:
                suffix = f"{parent.selector}." if parent.selector else ""
                return MetricInput(source=parent.source, selector=f"{suffix}{node.attr}")
        if isinstance(node, ast.Subscript):
            parent = self._path(node.value)
            if parent is not None:
                # Dynamic expressions are not field names and must not leak source.
                suffix = _static_index(node.slice) or "dynamic"
                return MetricInput(
                    source=parent.source, selector=f"{parent.selector}[{suffix}]"
                )
        return None

    def _read(self, node: ast.Attribute | ast.Subscript) -> None:
        path = self._path(node)
        if path is None:
            self.generic_visit(node)
            return
        if isinstance(node.ctx, ast.Load):
            self.reads[(path.source, path.selector)] = path
        # Visit index expressions separately, not the shorter prefixes of this read.
        current: ast.AST = node
        while isinstance(current, (ast.Attribute, ast.Subscript)):
            if isinstance(current, ast.Subscript):
                self.visit(current.slice)
            current = current.value

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            self._read(node)
        elif isinstance(node, ast.Call):
            # A called attribute is a method, not a selected field.
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)
            else:
                self.visit(node.func)
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            self.generic_visit(node)


def metric_inputs(selector: Callable[..., object]) -> tuple[MetricInput, ...]:
    """Describe only explicit reads in this selector, without invoking it or helpers."""
    node = _selector_node(selector)
    if node is not None:
        arguments = [*node.args.posonlyargs, *node.args.args]
        if arguments:
            parameter = arguments[0].arg
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            # A rebound context name no longer establishes context provenance.
            rebound = any(
                isinstance(child, ast.Name)
                and child.id == parameter
                and isinstance(child.ctx, (ast.Store, ast.Del))
                for statement in body
                for child in ast.walk(statement)
            )
            if not rebound:
                visitor = _ContextReads(parameter)
                for statement in body:
                    visitor.visit(statement)
                if visitor.reads:
                    return tuple(visitor.reads.values())
    return (MetricInput(source="custom", selector=_callable_identity(selector)),)
