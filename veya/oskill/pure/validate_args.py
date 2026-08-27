"""3O-PURE — validate_args: JSON Schema 绝对校验。

零依赖 JSON Schema 子集校验器（确定性、无 I/O、无随机）——幻觉拦截的
核心防线：LLM 产出的工具参数在这里被**绝对**校验，不合格即拒绝执行。

支持的关键字（覆盖工具参数 99% 场景）：
    type / required / properties / additionalProperties / items / minItems /
    maxItems / enum / minimum / maximum / exclusiveMinimum / exclusiveMaximum /
    minLength / maxLength / pattern / anyOf

另有 ``schema_of_legacy``：把旧 ``ToolMetadata.parameters`` 格式
（{param: {"required": bool, "type": "int|float|bool|str|list"}}）
转换为标准 JSON Schema，保证迁移期消息语义不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# JSON Schema 类型 → Python 判定
_TYPE_CHECKS: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}

_TYPE_NAMES: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


@dataclass
class ValidationResult:
    """校验结果：ok=False 时 errors 给出每条违规的路径化描述。"""

    ok: bool
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_value(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    """单值校验（schema 为具体 schema 对象，非 $ref 等）。"""
    if not isinstance(schema, dict):
        return

    # type
    type_spec = schema.get("type")
    if isinstance(type_spec, str):
        check = _TYPE_CHECKS.get(type_spec)
        if check is not None and not check(value):
            errors.append(f"{path}: 类型必须是 {type_spec}, 实际是 {_type_name(value)}")
            return  # 类型错就停止后续关键字（避免误报连环错误）
    elif isinstance(type_spec, list):
        if not any(_TYPE_CHECKS.get(t, lambda v: False)(value) for t in type_spec):
            errors.append(f"{path}: 类型必须是 {type_spec} 之一, 实际是 {_type_name(value)}")
            return

    # enum
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: 值 {value!r} 不在允许枚举 {schema['enum']} 内")
    # 数值边界
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")
    # 字符串长度/模式
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度 {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度 {len(value)} > maxLength {schema['maxLength']}")
        if "pattern" in schema:
            if re.search(str(schema["pattern"]), value) is None:
                errors.append(f"{path}: 不匹配 pattern {schema['pattern']!r}")
    # 数组
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 元素数 {len(value)} > maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                _check_value(item, schema["items"], f"{path}[{i}]", errors)
    # 对象
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for pname, ptype in props.items():
                if pname in value:
                    _check_value(value[pname], ptype, f"{path}.{pname}", errors)
        # required 独立于 properties 检查
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: 缺少必填字段 {req!r}")
        if schema.get("additionalProperties") is False and isinstance(props, dict):
            for key in value:
                if key not in props:
                    errors.append(f"{path}: 未知字段 {key!r} (additionalProperties=false)")
    # anyOf
    if "anyOf" in schema:
        variants = schema["anyOf"]
        if not any(_passes(value, v) for v in variants):
            errors.append(f"{path}: 不满足 anyOf 任一分支")
    # 字面量约束 (const)
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 值必须等于 {schema['const']!r}")


def _passes(value: Any, schema: Any) -> bool:
    """anyOf 分支判定：只跑 type/enum/const 等静态关键字。"""
    if not isinstance(schema, dict):
        return True
    if "type" in schema:
        type_spec = schema["type"]
        if isinstance(type_spec, str):
            check = _TYPE_CHECKS.get(type_spec)
            if check is not None and not check(value):
                return False
        elif isinstance(type_spec, list):
            if not any(_TYPE_CHECKS.get(t, lambda v: False)(value) for t in type_spec):
                return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    return True


def validate_args(args: Any, schema: dict) -> ValidationResult:
    """参数绝对校验：args 必须为对象，schema 为标准 JSON Schema。"""
    if not isinstance(args, dict):
        return ValidationResult(
            ok=False, errors=[f"参数必须是 JSON 对象, 实际是 {_type_name(args)}"]
        )
    errors: list[str] = []
    _check_value(args, schema, "$", errors)
    return ValidationResult(ok=not errors, errors=errors)


def schema_of_legacy(parameters: dict) -> dict:
    """旧 ToolMetadata.parameters → JSON Schema（迁移桥）。

    旧格式: {param: {"required": bool, "type": "int|float|bool|str|list"}}
    → 新格式: {type: "object", required: [...], properties: {param: {type: ...}}}
    """
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, spec in parameters.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {}
        type_spec = spec.get("type")
        if isinstance(type_spec, str) and type_spec in _TYPE_NAMES:
            entry["type"] = _TYPE_NAMES[type_spec]
        elif isinstance(type_spec, str):
            entry["type"] = type_spec  # 已是标准名则直通
        if entry:
            properties[name] = entry
        if spec.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


__all__ = [
    "ValidationResult",
    "schema_of_legacy",
    "validate_args",
]
