"""口径公式与填报校验：只允许数字、字段名和四则运算的安全求值。

公式在口径版本创建时校验（字段引用必须已声明），在提交与重算时求值。
填报值按口径的 inputs 规格校验：必填、数值类型、上下限；严格模式下
拒绝口径未定义的字段，宽松模式（重算迁移旧数据）忽略多余字段。
"""
from __future__ import annotations

import ast
import operator
import re
from typing import Any

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()


class FormulaError(ValueError):
    """公式或填报数据不合法。"""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
)


def validate_inputs_spec(spec: Any) -> list[str]:
    """校验口径 inputs 规格，返回字段名列表。规格形如：

    {"fields": [{"name": "rev", "required": true, "min": 0, "default": 0}, ...]}
    """
    if not isinstance(spec, dict):
        raise FormulaError("口径输入定义必须是对象")
    fields = spec.get("fields")
    if not isinstance(fields, list) or not fields:
        raise FormulaError("口径输入定义缺少 fields 列表")
    names: list[str] = []
    for index, field in enumerate(fields, start=1):
        if not isinstance(field, dict):
            raise FormulaError(f"第 {index} 个字段定义格式错误")
        name = field.get("name")
        if not isinstance(name, str) or not _FIELD_RE.match(name):
            raise FormulaError(f"第 {index} 个字段名不合法")
        if name in names:
            raise FormulaError(f"字段重复定义: {name}")
        for key in ("min", "max", "default"):
            if key in field and (isinstance(field[key], bool) or not isinstance(field[key], (int, float))):
                raise FormulaError(f"字段 {name} 的 {key} 必须是数字")
        if "min" in field and "max" in field and field["min"] > field["max"]:
            raise FormulaError(f"字段 {name} 的取值范围不合法")
        names.append(name)
    return names


def validate_formula(formula: Any, allowed_names: list[str]) -> None:
    """校验公式可解析、只含白名单元素、且引用的字段都已声明。"""
    tree = _parse(formula)
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    unknown = sorted(referenced - set(allowed_names))
    if unknown:
        raise FormulaError(f"公式引用了未定义的字段: {', '.join(unknown)}")


def coerce_values(spec: dict, raw: Any, strict: bool = True) -> dict[str, float]:
    """按口径 inputs 规格校验并规范化填报值，返回 {字段: 浮点值}。"""
    if not isinstance(raw, dict):
        raise FormulaError("填报数据必须是对象")
    fields = {field["name"]: field for field in spec["fields"]}
    if strict:
        unknown = sorted(set(raw) - set(fields))
        if unknown:
            raise FormulaError(f"包含口径未定义的字段: {', '.join(unknown)}")
    coerced: dict[str, float] = {}
    for name, field in fields.items():
        value = raw.get(name, field.get("default", _MISSING))
        if value is _MISSING:
            if field.get("required", True):
                raise FormulaError(f"缺少必填字段: {name}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FormulaError(f"字段 {name} 必须是数字")
        number = float(value)
        if "min" in field and number < field["min"]:
            raise FormulaError(f"字段 {name} 低于下限 {field['min']}")
        if "max" in field and number > field["max"]:
            raise FormulaError(f"字段 {name} 高于上限 {field['max']}")
        coerced[name] = number
    return coerced


def evaluate(formula: str, values: dict[str, float]) -> float:
    """对一组已校验的字段值求公式结果。"""
    tree = _parse(formula)
    return float(_eval(tree.body, values))


def _parse(formula: Any) -> ast.Expression:
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("公式不能为空")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"公式无法解析: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError("公式只允许数字、字段名与四则运算")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise FormulaError("公式中的常量必须是数字")
    return tree


def _eval(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaError("公式除数为零")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval(node.operand, values))
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"缺少字段: {node.id}")
        return float(values[node.id])
    if isinstance(node, ast.Constant):
        return float(node.value)
    raise FormulaError("公式包含不支持的元素")
