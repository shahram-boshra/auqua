# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""``AUQUA_`` environment-variable overrides (ADR 0018).

``AUQUA_PATHS__DATA_ROOT`` overrides ``paths.data_root``. Only fields marked
:class:`~auqua.config.base.Overridable` may be overridden; every other
``AUQUA_`` variable is refused, so a mistyped variable stops the load instead
of being ignored. Values are validated from strings by Pydantic for the
field's type.
"""

import copy
import types
import typing
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from auqua.config.base import Overridable
from auqua.config.errors import OverrideError

PREFIX = "AUQUA_"
DELIMITER = "__"


def _model_of(annotation: object) -> type[BaseModel] | None:
    """The model class inside ``annotation`` (``X`` or ``X | None``), else ``None``."""
    candidates = typing.get_args(annotation) if isinstance(annotation, types.UnionType) else ()
    for candidate in (annotation, *candidates):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _resolve(model: type[BaseModel], path: list[str], variable: str) -> Any:  # noqa: ANN401
    field: FieldInfo | None = None
    current: type[BaseModel] | None = model
    for depth, name in enumerate(path):
        if current is None or name not in current.model_fields:
            raise OverrideError(f"{variable}: no configuration field {'.'.join(path)}")
        field = current.model_fields[name]
        if depth < len(path) - 1:
            current = _model_of(field.annotation)
    if field is None or not any(isinstance(m, Overridable) for m in field.metadata):
        raise OverrideError(f"{variable}: field {'.'.join(path)} is not overridable")
    if field.annotation is None:  # pragma: no cover - every model field is annotated
        raise OverrideError(f"{variable}: field {'.'.join(path)} has no type")
    return field.annotation


def apply_overrides(
    model: type[BaseModel],
    document: Mapping[str, Any],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Return a copy of ``document`` with the ``AUQUA_`` overrides applied.

    Raises:
        OverrideError: a variable names an unknown or non-overridable field, or
            its value is invalid for the field's type.
    """
    result: dict[str, Any] = copy.deepcopy(dict(document))
    for variable in sorted(environ):
        if not variable.startswith(PREFIX):
            continue
        path = variable.removeprefix(PREFIX).lower().split(DELIMITER)
        annotation = _resolve(model, path, variable)
        try:
            value = TypeAdapter(annotation).validate_strings(environ[variable])
        except ValidationError as error:
            reasons = "; ".join(e["msg"] for e in error.errors())
            raise OverrideError(f"{variable}: invalid value ({reasons})") from error
        target = result
        for name in path[:-1]:
            target = target.setdefault(name, {})
            if not isinstance(target, dict):
                raise OverrideError(f"{variable}: {name} is not a table in the file")
        target[path[-1]] = value
    return result
