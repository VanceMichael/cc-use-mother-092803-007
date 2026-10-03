"""安全公式：仅允许四则运算、括号、指标引用与少量白名单函数。

公式形如 ``base * rate + adjustment``，其中标识符来自该指标依赖的
输入指标取值。公式在口径版本创建时编译，在上报校验/重算时求值。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_ALLOWED_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_ALLOWED_FUNCTIONS = {
    "max": max,
    "min": min,
    "round": round,
    "abs": abs,
}


class FormulaError(ValueError):
    """公式非法（含禁用语法或未知引用）。"""


def extract_references(expression: str) -> list[str]:
    """返回公式中引用的输入指标编码，按首次出现排序。"""
    tree = _parse(expression)
    refs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in refs:
            refs.append(node.id)
    return refs


def compile_formula(expression: str, known_references: set[str] | None = None) -> ast.Expression:
    """编译并校验公式；known_references 给出时检查引用是否已声明。"""
    tree = _parse(expression)
    for node in ast.walk(tree):
        _node_supported(node)  # 编译期即拒绝属性访问、调用逃逸等危险语法
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise FormulaError("公式常量只能是数字")
        if isinstance(node, ast.Name) and known_references is not None and node.id not in known_references:
            raise FormulaError(f"公式引用了未声明的输入指标: {node.id}")
    return tree


def evaluate(expression_or_tree: str | ast.Expression, values: dict[str, float]) -> float:
    """按给定输入值计算公式结果，缺引用或除零均抛出 FormulaError。"""
    tree = _parse(expression_or_tree) if isinstance(expression_or_tree, str) else expression_or_tree
    try:
        return float(_eval(tree.body, values))
    except FormulaError:
        raise
    except ZeroDivisionError as exc:
        raise FormulaError("公式计算时发生除零") from exc
    except ArithmeticError as exc:
        raise FormulaError(f"公式计算失败: {exc}") from exc


def _parse(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("公式不能为空")
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"公式语法错误: {exc.msg}") from exc


# 允许出现的节点类型（运算符、ctx 等叶节点不在此列，按结构化遍历校验）
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub,
)


def _node_supported(node: ast.AST) -> None:
    if isinstance(node, _ALLOWED_NODES):
        # 以下划线开头的名字可触及魔术属性/模块内部，一律禁止
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise FormulaError(f"公式不允许引用内部名称: {node.id}")
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCTIONS:
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            raise FormulaError("函数调用形式不被允许")
        return
    raise FormulaError(f"公式包含不允许的语法: {type(node).__name__}")


def _eval(node: ast.AST, values: dict[str, float]) -> float:
    _node_supported(node)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("公式常量只能是数字")
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"缺少输入指标取值: {node.id}")
        return values[node.id]
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise FormulaError("不支持的一元运算")
        return op(_eval(node.operand, values))
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise FormulaError("不支持的二元运算")
        return op(_eval(node.left, values), _eval(node.right, values))
    if isinstance(node, ast.Call):
        args = [_eval(arg, values) for arg in node.args]
        return _ALLOWED_FUNCTIONS[node.func.id](*args)
    raise FormulaError(f"不支持的节点: {type(node).__name__}")
