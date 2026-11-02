"""Bounded, non-executing SDK declaration inspection and offline input checks."""

import ast
import json
import re
from pathlib import Path
from typing import Any

from .config import SetupError

CONNECTION_TYPES = {
    "DHIS2Connection",
    "IASOConnection",
    "PostgreSQLConnection",
    "GCSConnection",
    "S3Connection",
    "CustomConnection",
}
REFERENCE_TYPES = CONNECTION_TYPES | {"Dataset", "File"}
SUPPORTED_TYPES = REFERENCE_TYPES | {"str", "int", "float", "bool", "Secret"}
LIMITATIONS = (
    "Offline checks only: workspace existence, visibility and connection type were not "
    "checked. Dataset references are workspace slugs, not owner-qualified conversions "
    "or version pins. File references are workspace files, not dataset-version files "
    "or local paths. SDK runtime validation remains authoritative; this does not prove "
    "execution is safe or will succeed."
)


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _sdk_names(tree: ast.Module, name: str) -> set[str]:
    names = {name}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("openhexa.sdk")
        ):
            names.update(
                alias.asname or alias.name for alias in node.names if alias.name == name
            )
    return names


def reject_secret_overrides(path: Path, codes: set[str]) -> None:
    """Reject recognizable Secret inputs without gating execution on parser support.

    Only newly supplied file/CLI inputs are checked. This is not a secret scanner or
    protection for existing configured values, computed types, arbitrary code or logs.
    """
    if not codes:
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    secret_names = _sdk_names(tree, "Secret")
    parameter_names = _sdk_names(tree, "parameter")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _name(node.func) not in parameter_names:
            continue
        keywords = {item.arg: item.value for item in node.keywords}
        code = node.args[0] if node.args else keywords.get("code")
        if (
            isinstance(code, ast.Constant)
            and isinstance(code.value, str)
            and code.value in codes
            and _name(keywords.get("type", ast.Constant(None))) in secret_names
        ):
            raise SetupError(
                "Secret parameter overrides are unsupported; do not put secrets in "
                "CLI arguments or ordinary parameter files."
            )


def inspect_pipeline(path: Path) -> dict[str, Any]:
    """Read conventional decorators and literal metadata, never imports or expressions.

    Unsupported declarations yield location-bearing diagnostics and a nonzero result,
    retaining any discoverable metadata. Secret defaults and choices are suppressed.
    `pipeline_diagnostics` is the subset concerning the `@pipeline` declaration
    itself rather than its parameters.
    """
    diagnostics: list[str] = []
    pipeline_diagnostics: list[str] = []

    def diagnostic(node: ast.AST, message: str, pipeline_level: bool = False) -> None:
        text = (
            f"{path}:{getattr(node, 'lineno', 1)}:"
            f"{getattr(node, 'col_offset', 0) + 1}: {message}"
        )
        diagnostics.append(text)
        if pipeline_level:
            pipeline_diagnostics.append(text)

    def literal(node: ast.AST) -> Any:
        if not isinstance(node, (ast.Constant, ast.List, ast.Tuple, ast.UnaryOp)):
            raise TypeError("not literal")
        value = ast.literal_eval(node)
        json.dumps(value, allow_nan=False)
        return value

    def metadata(call: ast.Call, positional: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if call.args:
            if len(call.args) != 1:
                diagnostic(call, "Use one literal positional argument or keywords.")
            values[positional] = call.args[0]
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg in values:
                diagnostic(
                    call, "Expanded or duplicate decorator keywords are unsupported."
                )
            else:
                values[keyword.arg] = keyword.value
        return values

    result: dict[str, Any] = {
        "entrypoint": str(path),
        "pipeline": None,
        "parameters": [],
        "diagnostics": diagnostics,
        "pipeline_diagnostics": pipeline_diagnostics,
        "limitations": LIMITATIONS,
    }
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        message = f"{path}:{error.lineno or 1}:{error.offset or 1}: Invalid Python syntax; fix the entrypoint before inspection."
        diagnostics.append(message)
        pipeline_diagnostics.append(message)
    else:
        pipeline_names = _sdk_names(tree, "pipeline")
        parameter_names = _sdk_names(tree, "parameter")
        secret_names = _sdk_names(tree, "Secret")
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(item, ast.Call)
                and _name(item.func) in pipeline_names | parameter_names
                for item in node.decorator_list
            )
        ]
        if len(candidates) != 1:
            diagnostic(
                candidates[0] if candidates else tree,
                "Expected one pipeline function; use conventional unaliased @pipeline and @parameter decorators.",
                True,
            )
        else:
            function = candidates[0]
            if function not in tree.body or isinstance(function, ast.AsyncFunctionDef):
                diagnostic(
                    function,
                    "Only a top-level synchronous pipeline function is supported.",
                    True,
                )
            result["pipeline"] = {"function": function.name, "line": function.lineno}
            pipeline_count = 0
            for decorator in function.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Name)
                    or decorator.func.id not in {"pipeline", "parameter"}
                ):
                    diagnostic(
                        decorator,
                        "Use conventional unaliased SDK decorators; custom/computed decorators are unsupported.",
                        True,
                    )
                    continue
                is_pipeline = decorator.func.id == "pipeline"
                values = metadata(decorator, "code")
                if is_pipeline:
                    pipeline_count += 1
                    for key, node in values.items():
                        if key not in {"name", "code", "timeout", "functional_type"}:
                            diagnostic(
                                node, "Unsupported pipeline metadata keyword.", True
                            )
                            continue
                        try:
                            value = literal(node)
                        except (ValueError, TypeError):
                            diagnostic(
                                node,
                                "Computed pipeline metadata is unsupported; use literals.",
                                True,
                            )
                            continue
                        expected = int if key == "timeout" else str
                        if type(value) is not expected:
                            diagnostic(
                                node,
                                f"Pipeline {key} must be a literal {expected.__name__}.",
                                True,
                            )
                        else:
                            result["pipeline"][key] = value
                    continue
                parameter: dict[str, Any] = {
                    "code": None,
                    "type": None,
                    "required": True,
                    "multiple": False,
                    "default": None,
                    "help": None,
                    "choices": None,
                    "line": decorator.lineno,
                }
                type_node = values.get("type")
                secret = type_node is not None and _name(type_node) in secret_names
                if isinstance(type_node, ast.Name) and type_node.id in SUPPORTED_TYPES:
                    parameter["type"] = type_node.id
                else:
                    diagnostic(
                        decorator, "Use a supported unaliased SDK parameter type."
                    )
                for key, node in values.items():
                    if key == "type":
                        continue
                    if secret and key in {"default", "choices"}:
                        parameter[key] = "[withheld]"
                        continue
                    if key not in {
                        "code",
                        "name",
                        "required",
                        "multiple",
                        "default",
                        "help",
                        "choices",
                    }:
                        diagnostic(
                            node,
                            "Widgets, dependencies and other advanced metadata are not checked offline; use SDK runtime validation.",
                        )
                        continue
                    try:
                        value = literal(node)
                        parameter[key] = (
                            list(value)
                            if key == "choices" and isinstance(value, tuple)
                            else value
                        )
                    except (ValueError, TypeError):
                        diagnostic(
                            node,
                            f"Computed {key} is unsupported; use literal metadata.",
                        )
                code = parameter["code"]
                if not isinstance(code, str) or not re.fullmatch(
                    r"[a-z_][a-z_0-9]+", code
                ):
                    diagnostic(
                        decorator,
                        "Parameter code must be a literal SDK identifier (lowercase letters, digits and underscores).",
                    )
                if any(p["code"] == code for p in result["parameters"]):
                    diagnostic(decorator, "Duplicate parameter code.")
                if any(
                    type(parameter[key]) is not bool for key in ("required", "multiple")
                ):
                    diagnostic(
                        decorator, "required and multiple must be literal booleans."
                    )
                if not all(
                    parameter[key] is None or isinstance(parameter[key], str)
                    for key in ("help", "name")
                    if key in parameter
                ):
                    diagnostic(
                        decorator,
                        "Parameter name/help must be literal strings or null.",
                    )
                kind = parameter["type"]
                if secret:
                    diagnostic(
                        decorator,
                        "Secret parameters are outside offline validation; defaults are withheld. Never supply secrets in CLI arguments, ordinary parameter files or logs.",
                    )
                elif kind is not None:
                    choices = parameter["choices"]
                    if choices is not None and (
                        not isinstance(choices, list) or not choices
                    ):
                        diagnostic(
                            decorator,
                            "Only nonempty literal choices lists/tuples are supported; file-backed/dynamic choices are not checked.",
                        )
                    elif choices is not None and any(
                        not _valid_type(kind, v) for v in choices
                    ):
                        diagnostic(
                            decorator, "Choice types do not match the parameter type."
                        )
                    if kind in CONNECTION_TYPES | {"bool", "File"} and (
                        parameter["multiple"] or choices is not None
                    ):
                        diagnostic(
                            decorator,
                            "This SDK type does not support multiple values or choices.",
                        )
                    if kind == "Dataset" and choices is not None:
                        diagnostic(
                            decorator,
                            "Dataset choices need SDK resource resolution and are not checked offline.",
                        )
                    default = parameter["default"]
                    if default is not None:
                        defaults = (
                            default
                            if parameter["multiple"] and isinstance(default, list)
                            else [default]
                        )
                        if parameter["multiple"] and not isinstance(default, list):
                            diagnostic(
                                decorator,
                                "A multiple parameter default must be a literal list.",
                            )
                        if any(not _valid_type(kind, v) or v == "" for v in defaults):
                            diagnostic(
                                decorator,
                                "Default types do not match the parameter type.",
                            )
                        if isinstance(choices, list) and any(
                            v not in choices for v in defaults
                        ):
                            diagnostic(
                                decorator, "Default is outside the literal choices."
                            )
                result["parameters"].append(parameter)
            if pipeline_count != 1:
                diagnostic(
                    function,
                    "Expected exactly one conventional @pipeline decorator.",
                    True,
                )
            identity: dict[str, Any] = result["pipeline"]
            identity["name"] = identity.get("name") or identity.get("code")
    result["supported"] = not diagnostics
    result["exit_code"] = 2 if diagnostics else 0
    if diagnostics:
        result.update(
            code="UNSUPPORTED_DECLARATION",
            message="Static inspection is incomplete; address the diagnostics or use SDK runtime validation.",
        )
    return result


def _valid_type(kind: str, value: Any) -> bool:
    if kind in REFERENCE_TYPES or kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int)
    if kind == "float":
        return isinstance(value, float)
    return isinstance(value, bool) if kind == "bool" else False


def _normalize(kind: str, value: Any) -> Any:
    if kind == "str" and isinstance(value, str):
        return value.strip() or None
    if kind == "float" and isinstance(value, int):
        return float(value)
    return value


def validate_parameters(
    schema: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    """Check supported inputs using SDK 2.23.0 omission semantics, without resolving resources.

    Never return inputs or normalized/defaulted values. The caller's explicit dictionary
    is untouched, so runtime normalization remains the SDK's responsibility.
    """
    diagnostics = list(schema["diagnostics"])
    if schema["supported"]:
        codes = {parameter["code"] for parameter in schema["parameters"]}
        if unknown := sorted(values.keys() - codes):
            diagnostics.append(
                f"Unknown parameter code(s): {', '.join(unknown)}; compare input keys "
                "with hexa inspect (local) or hexa pipelines show (deployed)."
            )
        for parameter in schema["parameters"]:
            code, kind = parameter["code"], parameter["type"]
            value = values.get(code)
            multiple = parameter["multiple"]
            if multiple:
                if value is not None and not isinstance(value, list):
                    diagnostics.append(f"Parameter {code}: expected a list.")
                    continue
                normalized = [_normalize(kind, v) for v in (value or [])]
                value = [v for v in normalized if v is not None]
                if not value and parameter["default"] is not None:
                    value = parameter["default"]
                missing = not value
            else:
                value = _normalize(kind, value)
                if value is None:
                    value = parameter["default"]
                if value is None and kind == "bool":
                    value = False
                missing = value is None
            if missing:
                if parameter["required"]:
                    diagnostics.append(
                        f"Parameter {code}: a value or nonempty default is required."
                    )
                continue
            checked = value if multiple else [value]
            if any(not _valid_type(kind, v) for v in checked):
                diagnostics.append(
                    f"Parameter {code}: expected {kind}{' list items' if multiple else ''}."
                )
            elif parameter["choices"] is not None and any(
                v not in parameter["choices"] for v in checked
            ):
                diagnostics.append(
                    f"Parameter {code}: value is outside the literal choices."
                )
    result = {
        "entrypoint": schema["entrypoint"],
        "pipeline": schema["pipeline"],
        "valid": not diagnostics,
        "diagnostics": diagnostics,
        "limitations": LIMITATIONS,
        "exit_code": 2 if diagnostics else 0,
    }
    if diagnostics:
        result.update(
            code="INVALID_INPUT" if schema["supported"] else "UNSUPPORTED_DECLARATION",
            message="Offline validation failed or is incomplete; see diagnostics.",
        )
    return result
