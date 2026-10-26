"""Registry-driven metric planning, validation, compilation, and normalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pglast.ast import Node
from pglast.enums import A_Expr_Kind, BoolExprType
from pglast.parser import parse_sql

MAX_RANGE_DAYS = 90
MAX_RESULT_ROWS = 100
ZERO_BASELINE = "ZERO_BASELINE"
DATA_INSUFFICIENT = "DATA_INSUFFICIENT"

_MONEY_QUANTUM = Decimal("0.01")
_RATE_QUANTUM = Decimal("0.0001")

# Registry expression keys select immutable platform-owned templates. Registry
# text is data only; it is never interpreted as SQL or Python.
_EXPRESSION_SQL = {
    "paid_amount_sum": ("SUM(paid_amount)", "sum(paid_amount)"),
}
_SOURCE_COLUMNS = {
    "business.gmv_v1": frozenset(
        {"data_snapshot_id", "paid_amount", "paid_at", "region_code"}
    )
}
_SOURCE_ALLOWED_DIMENSIONS = {
    "business.gmv_v1": frozenset({"region_code"}),
}
_SOURCE_ALLOWED_FILTERS = {
    "business.gmv_v1": frozenset({"region_code", "paid_at"}),
}
_SOURCE_EXPRESSIONS = {
    "business.gmv_v1": frozenset({"paid_amount_sum"}),
}
_ALLOWED_SQL_NODES = frozenset({
    "A_Const",
    "A_Expr",
    "BoolExpr",
    "CoalesceExpr",
    "ColumnRef",
    "FuncCall",
    "Integer",
    "ParamRef",
    "RangeVar",
    "ResTarget",
    "SelectStmt",
    "String",
    "TypeCast",
    "TypeName",
})
_ALLOWED_SQL_FUNCTIONS = frozenset({"coalesce", "count", "sum"})
_ALLOWED_SQL_OPERATORS = frozenset({"<", ">=", "="})


class QueryPlanError(ValueError):
    """A structured plan is outside the registered metric schema."""

    code = "QUERY_PLAN_INVALID"


class SQLValidationError(ValueError):
    """Compiled SQL fails the PostgreSQL AST allowlist."""


@dataclass(frozen=True)
class MetricDefinition:
    metric_key: str
    version: int
    source_view: str
    expression_key: str
    allowed_dimensions: frozenset[str]
    allowed_filters: frozenset[str]
    aggregation: str
    unit: str
    time_column: str
    status: str = "active"

    @classmethod
    def from_row(cls, row: Any) -> MetricDefinition:
        return cls(
            metric_key=row["metric_key"],
            version=row["version"],
            source_view=row["source_view"],
            expression_key=row["expression_key"],
            allowed_dimensions=frozenset(row["allowed_dimensions"]),
            allowed_filters=frozenset(row["allowed_filters"]),
            aggregation=row["aggregation"],
            unit=row["unit"],
            time_column=row["time_column"],
            status=row["status"],
        )


@dataclass(frozen=True)
class MetricIntent:
    metric: str
    metric_version: int
    dimensions: tuple[str, ...]
    filters: dict[str, Any]
    time_range: dict[str, Any]
    comparison: str
    limit: int


@dataclass(frozen=True)
class ValidatedMetricPlan:
    definition: MetricDefinition
    dimensions: tuple[str, ...]
    regions: tuple[str, ...]
    snapshot_id: str
    current_start: datetime
    current_end: datetime
    previous_start: datetime
    previous_end: datetime
    comparison: str
    limit: int


@dataclass(frozen=True)
class CompiledMetricQuery:
    sql: str
    parameters: tuple[Any, ...]
    query_plan: dict[str, Any]


@dataclass(frozen=True)
class MetricResult:
    current_value: Decimal
    previous_value: Decimal
    absolute_change: Decimal
    change_rate: Decimal | None
    row_count: int
    previous_row_count: int
    dimension_results: tuple[dict[str, Any], ...]
    annotations: tuple[str, ...]


@dataclass(frozen=True)
class GmvDelta:
    current_value: Decimal
    previous_value: Decimal
    absolute_change: Decimal
    change_rate: Decimal | None
    current_row_count: int
    previous_row_count: int
    annotations: tuple[str, ...]


def _require_iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise QueryPlanError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise QueryPlanError(f"{field} must be an ISO date") from None
    if parsed.isoformat() != value:
        raise QueryPlanError(f"{field} must use YYYY-MM-DD")
    return parsed


def _require_timezone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value:
        raise QueryPlanError("time_range.timezone must name an IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise QueryPlanError(
            "time_range.timezone must name an IANA timezone"
        ) from None


def _require_limit(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_RESULT_ROWS
    ):
        raise QueryPlanError(
            f"limit must be an integer between 1 and {MAX_RESULT_ROWS}"
        )
    return value


class MetricPlanner:
    """Parse only the structured, non-SQL Metric Query Plan contract."""

    _FIELDS = frozenset({
        "metric", "metric_version", "dimensions", "filters", "time_range",
        "comparison", "limit",
    })

    @classmethod
    def plan(cls, plan: Any) -> MetricIntent:
        if not isinstance(plan, dict) or set(plan) != cls._FIELDS:
            raise QueryPlanError("query plan has unknown or missing fields")
        metric = plan["metric"]
        version = plan["metric_version"]
        if not isinstance(metric, str) or not metric:
            raise QueryPlanError("metric must be a non-empty registered key")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise QueryPlanError("metric_version must be a positive integer")
        dimensions = plan["dimensions"]
        if not isinstance(dimensions, list) or any(
            not isinstance(item, str) for item in dimensions
        ):
            raise QueryPlanError("dimensions must be a list of strings")
        if len(dimensions) > 4:
            raise QueryPlanError("dimensions may contain at most four entries")
        filters = plan["filters"]
        time_range = plan["time_range"]
        if not isinstance(filters, dict):
            raise QueryPlanError("filters must be an object")
        if not isinstance(time_range, dict) or set(time_range) != {
            "start", "end_exclusive", "timezone"
        }:
            raise QueryPlanError("time_range has the wrong shape")
        comparison = plan["comparison"]
        if not isinstance(comparison, str):
            raise QueryPlanError("comparison must be a string")
        return MetricIntent(
            metric=metric,
            metric_version=version,
            dimensions=tuple(dict.fromkeys(dimensions)),
            filters=filters,
            time_range=time_range,
            comparison=comparison,
            limit=_require_limit(plan["limit"]),
        )


class SemanticValidator:
    """Validate plan semantics against one active registered metric definition."""

    @staticmethod
    def validate(
        intent: MetricIntent,
        definition: MetricDefinition | None,
        *,
        region_scope: tuple[str, ...],
        data_snapshot_id: str,
    ) -> ValidatedMetricPlan:
        if definition is None or definition.status != "active":
            raise QueryPlanError("metric is not registered and active")
        if (intent.metric, intent.metric_version) != (
            definition.metric_key, definition.version
        ):
            raise QueryPlanError("metric version is not registered")
        if intent.comparison != "previous_period":
            raise QueryPlanError("comparison mode is not supported")
        source = definition.source_view
        if source not in _SOURCE_COLUMNS:
            raise QueryPlanError("metric source view is outside platform policy")
        if any(
            item not in definition.allowed_dimensions
            or item not in _SOURCE_ALLOWED_DIMENSIONS[source]
            for item in intent.dimensions
        ):
            raise QueryPlanError("dimensions are outside the registered schema")
        registered_expression = _EXPRESSION_SQL.get(definition.expression_key)
        expected_aggregation = (
            "SUM" if definition.expression_key == "paid_amount_sum" else None
        )
        if (
            registered_expression is None
            or definition.expression_key not in _SOURCE_EXPRESSIONS[source]
            or definition.aggregation != expected_aggregation
            or definition.source_view != "business.gmv_v1"
            or definition.time_column != "paid_at"
            or registered_expression[0] != "SUM(paid_amount)"
        ):
            raise QueryPlanError("metric definition is outside platform policy")

        # Region values are the user-selectable filters. The registered time
        # column is supplied only via the typed time_range above.
        if set(intent.filters) != {"region_code"}:
            raise QueryPlanError("filters are outside the registered schema")
        if (
            not {"region_code", definition.time_column}.issubset(
                definition.allowed_filters
            )
            or not definition.allowed_filters.issubset(
                _SOURCE_ALLOWED_FILTERS[source]
            )
        ):
            raise QueryPlanError("filters are outside the registered schema")
        requested = intent.filters["region_code"]
        if not isinstance(requested, list) or not requested or any(
            not isinstance(item, str) or not item.strip() for item in requested
        ):
            raise QueryPlanError(
                "filters.region_code must contain non-empty strings"
            )
        requested_regions = sorted({item.strip() for item in requested})

        time_range = intent.time_range
        start = _require_iso_date(time_range["start"], "time_range.start")
        end = _require_iso_date(
            time_range["end_exclusive"], "time_range.end_exclusive"
        )
        zone = _require_timezone(time_range["timezone"])
        duration_days = (end - start).days
        if duration_days <= 0 or duration_days > MAX_RANGE_DAYS:
            raise QueryPlanError(
                f"date range must be between 1 and {MAX_RANGE_DAYS} calendar days"
            )
        scope = {region.strip() for region in region_scope if region.strip()}
        regions = tuple(region for region in requested_regions if region in scope)
        if not regions:
            raise QueryPlanError(
                "no authorized region remains after applying the Task scope"
            )
        if not isinstance(data_snapshot_id, str) or not data_snapshot_id:
            raise QueryPlanError("task data snapshot is required")

        current_start = datetime.combine(start, time.min, tzinfo=zone)
        current_end = datetime.combine(end, time.min, tzinfo=zone)
        previous_start = datetime.combine(
            start - timedelta(days=duration_days), time.min, tzinfo=zone
        )
        return ValidatedMetricPlan(
            definition=definition,
            dimensions=intent.dimensions,
            regions=regions,
            snapshot_id=data_snapshot_id,
            current_start=current_start,
            current_end=current_end,
            previous_start=previous_start,
            previous_end=current_start,
            comparison=intent.comparison,
            limit=intent.limit,
        )


class SQLValidationError(ValueError):
    """Compiled SQL fails the PostgreSQL AST allowlist."""


def _walk_ast(value: Any) -> Iterator[Node]:
    if isinstance(value, Node):
        yield value
        for name in type(value).__slots__:
            child = getattr(value, name)
            if isinstance(child, (Node, list, tuple)):
                yield from _walk_ast(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_ast(child)


def _is_column(node: Any, name: str) -> bool:
    return (
        type(node).__name__ == "ColumnRef"
        and len(node.fields) == 1
        and type(node.fields[0]).__name__ == "String"
        and node.fields[0].sval == name
    )


def _is_param(node: Any, number: int) -> bool:
    return type(node).__name__ == "ParamRef" and node.number == number


def _is_text_array_param(node: Any, number: int) -> bool:
    if type(node).__name__ != "TypeCast" or not _is_param(node.arg, number):
        return False
    type_name = node.typeName
    return (
        tuple(part.sval for part in type_name.names
              if type(part).__name__ == "String") == ("text",)
        and len(type_name.arrayBounds) == 1
    )


def _is_comparison(
    node: Any, operator: str, left: Any, right: Any, *, kind: A_Expr_Kind
) -> bool:
    return (
        type(node).__name__ == "A_Expr"
        and node.kind == kind
        and len(node.name) == 1
        and type(node.name[0]).__name__ == "String"
        and node.name[0].sval == operator
        and left(node.lexpr)
        and right(node.rexpr)
    )


def _is_and(node: Any, *predicates: Any) -> bool:
    return (
        type(node).__name__ == "BoolExpr"
        and node.boolop == BoolExprType.AND_EXPR
        and len(node.args) == len(predicates)
        and all(
            predicate(actual)
            for predicate, actual in zip(predicates, node.args, strict=True)
        )
    )


def _is_function_named(node: Any, name: str) -> bool:
    return (
        type(node).__name__ == "FuncCall"
        and tuple(
            part.sval for part in node.funcname
            if type(part).__name__ == "String"
        ) == (name,)
    )


def _is_period_filter(node: Any, start_param: int, end_param: int) -> bool:
    return _is_and(
        node,
        lambda item: _is_comparison(
            item, ">=", lambda expr: _is_column(expr, "paid_at"),
            lambda expr: _is_param(expr, start_param), kind=A_Expr_Kind.AEXPR_OP,
        ),
        lambda item: _is_comparison(
            item, "<", lambda expr: _is_column(expr, "paid_at"),
            lambda expr: _is_param(expr, end_param), kind=A_Expr_Kind.AEXPR_OP,
        ),
    )


def _is_zero_numeric(node: Any) -> bool:
    return (
        type(node).__name__ == "TypeCast"
        and tuple(
            part.sval for part in node.typeName.names
            if type(part).__name__ == "String"
        ) == ("pg_catalog", "numeric")
        and type(node.arg).__name__ == "A_Const"
        and not node.arg.isnull
        and type(node.arg.val).__name__ == "Integer"
        and node.arg.val.ival == 0
    )


def _is_sum_period(node: Any, start_param: int, end_param: int) -> bool:
    return (
        _is_function_named(node, "sum")
        and len(node.args or ()) == 1
        and _is_column(node.args[0], "paid_amount")
        and _is_period_filter(node.agg_filter, start_param, end_param)
        and not node.agg_star
        and not node.agg_distinct
        and not node.agg_order
        and node.over is None
    )


def _is_count_period(node: Any, start_param: int, end_param: int) -> bool:
    return (
        _is_function_named(node, "count")
        and node.agg_star
        and not node.args
        and _is_period_filter(node.agg_filter, start_param, end_param)
        and not node.agg_distinct
        and not node.agg_order
        and node.over is None
    )


def _is_coalesced_sum_period(
    node: Any, start_param: int, end_param: int
) -> bool:
    return (
        type(node).__name__ == "CoalesceExpr"
        and len(node.args) == 2
        and _is_sum_period(node.args[0], start_param, end_param)
        and _is_zero_numeric(node.args[1])
    )


class SQLValidator:
    """Validate compiled output as one allowlisted PostgreSQL SELECT AST."""

    @classmethod
    def validate(
        cls,
        sql: str,
        definition: MetricDefinition,
        dimensions: tuple[str, ...] = (),
    ) -> None:
        try:
            statements = parse_sql(sql)
        except Exception as error:
            raise SQLValidationError("compiled SQL is not valid PostgreSQL") from error
        if len(statements) != 1 or type(statements[0].stmt).__name__ != "SelectStmt":
            raise SQLValidationError("compiled SQL must contain exactly one SELECT")

        statement = statements[0].stmt
        if (
            statement.intoClause is not None
            or statement.withClause is not None
            or statement.lockingClause
            or statement.valuesLists
            or statement.havingClause is not None
            or statement.windowClause
            or statement.sortClause
            or statement.limitOffset is not None
            or statement.op.value != 0
            or len(statement.fromClause or ()) != 1
            or statement.distinctClause
            or len(statement.targetList or ()) != len(dimensions) + 4
        ):
            raise SQLValidationError("compiled SELECT uses an unsupported clause")
        source = statement.fromClause[0]
        if (
            type(source).__name__ != "RangeVar"
            or source.catalogname is not None
            or f"{source.schemaname}.{source.relname}" != definition.source_view
            or source.alias is not None
        ):
            raise SQLValidationError("compiled SQL reads an unsupported relation")
        columns = _SOURCE_COLUMNS.get(definition.source_view)
        if columns is None:
            raise SQLValidationError("registered source view has no platform policy")

        targets = tuple(statement.targetList or ())
        expected_outputs = (
            *dimensions,
            "current_value", "previous_value",
            "current_row_count", "previous_row_count",
        )
        actual_outputs = tuple(
            target.name
            or (
                target.val.fields[0].sval
                if type(target.val).__name__ == "ColumnRef"
                and len(target.val.fields) == 1
                and type(target.val.fields[0]).__name__ == "String"
                else None
            )
            for target in targets
        )
        if actual_outputs != expected_outputs:
            raise SQLValidationError("compiled SQL result columns are unsupported")
        dimension_targets = targets[: len(dimensions)]
        if any(
            not _is_column(target.val, dimension)
            for target, dimension in zip(dimension_targets, dimensions, strict=True)
        ):
            raise SQLValidationError("compiled SQL dimensions are unsupported")
        metric_targets = targets[len(dimensions) :]
        if not (
            _is_coalesced_sum_period(metric_targets[0].val, 5, 6)
            and _is_coalesced_sum_period(metric_targets[1].val, 7, 8)
            and _is_count_period(metric_targets[2].val, 5, 6)
            and _is_count_period(metric_targets[3].val, 7, 8)
        ):
            raise SQLValidationError("compiled SQL metric expressions are unsupported")
        if (
            statement.limitCount is None
            or type(statement.limitCount).__name__ != "ParamRef"
            or statement.limitCount.number != 9
        ):
            raise SQLValidationError("compiled SQL must bind the row limit")

        if not _is_and(
            statement.whereClause,
            lambda node: _is_comparison(
                node,
                "=",
                lambda value: _is_column(value, "data_snapshot_id"),
                lambda value: _is_param(value, 1),
                kind=A_Expr_Kind.AEXPR_OP,
            ),
            lambda node: _is_comparison(
                node,
                "=",
                lambda value: _is_column(value, "region_code"),
                lambda value: _is_text_array_param(value, 2),
                kind=A_Expr_Kind.AEXPR_OP_ANY,
            ),
            lambda node: _is_comparison(
                node, ">=", lambda value: _is_column(value, definition.time_column),
                lambda value: _is_param(value, 3), kind=A_Expr_Kind.AEXPR_OP,
            ),
            lambda node: _is_comparison(
                node, "<", lambda value: _is_column(value, definition.time_column),
                lambda value: _is_param(value, 4), kind=A_Expr_Kind.AEXPR_OP,
            ),
        ):
            raise SQLValidationError("compiled SQL scope predicates are unsupported")

        parameter_numbers: set[int] = set()
        expected_columns = {
            "data_snapshot_id": 1,
            "region_code": 1,
            definition.time_column: 10,
        }
        for dimension in dimensions:
            expected_columns[dimension] = expected_columns.get(dimension, 0) + 2
        group_clause = tuple(statement.groupClause or ())
        if len(group_clause) != len(dimensions) or any(
            type(item).__name__ != "ColumnRef"
            or tuple(field.sval for field in item.fields
                     if type(field).__name__ == "String") != (dimension,)
            for item, dimension in zip(group_clause, dimensions, strict=True)
        ):
            raise SQLValidationError("compiled SQL grouping is unsupported")

        for node in _walk_ast(statement):
            node_type = type(node).__name__
            if node_type not in _ALLOWED_SQL_NODES:
                raise SQLValidationError(
                    f"compiled SQL contains unsupported AST node {node_type}"
                )
            if node_type == "ParamRef":
                if not 1 <= node.number <= 9:
                    raise SQLValidationError("compiled SQL has an unsupported parameter")
                parameter_numbers.add(node.number)
            elif node_type == "A_Const":
                value = node.val
                if (
                    type(value).__name__ != "Integer"
                    or value.ival != 0
                    or node.isnull
                ):
                    raise SQLValidationError(
                        "compiled SQL contains an unbound constant"
                    )
            elif node_type == "ColumnRef":
                fields = tuple(
                    field.sval for field in node.fields
                    if type(field).__name__ == "String"
                )
                if len(fields) != 1 or fields[0] not in columns:
                    raise SQLValidationError(
                        "compiled SQL references an unsupported column"
                    )
                if fields[0] in expected_columns:
                    expected_columns[fields[0]] -= 1
            elif node_type == "FuncCall":
                names = tuple(
                    part.sval for part in node.funcname
                    if type(part).__name__ == "String"
                )
                if (
                    len(names) != 1
                    or names[0].lower() not in _ALLOWED_SQL_FUNCTIONS
                    or node.agg_distinct
                    or node.agg_within_group
                    or node.func_variadic
                    or node.over is not None
                    or node.agg_order
                    or names[0].lower() == "sum" and len(node.args or ()) != 1
                    or names[0].lower() == "coalesce" and len(node.args or ()) != 2
                    or names[0].lower() == "count" and not node.agg_star
                ):
                    raise SQLValidationError(
                        "compiled SQL calls an unsupported function"
                    )
            elif node_type == "A_Expr":
                operators = tuple(
                    part.sval for part in node.name
                    if type(part).__name__ == "String"
                )
                if (
                    node.kind not in {A_Expr_Kind.AEXPR_OP, A_Expr_Kind.AEXPR_OP_ANY}
                    or len(operators) != 1
                    or operators[0] not in _ALLOWED_SQL_OPERATORS
                ):
                    raise SQLValidationError(
                        "compiled SQL uses an unsupported operator"
                    )
            elif node_type == "BoolExpr" and node.boolop != BoolExprType.AND_EXPR:
                raise SQLValidationError(
                    "compiled SQL uses an unsupported boolean operator"
                )
            elif node_type == "TypeName":
                names = tuple(
                    part.sval for part in node.names
                    if type(part).__name__ == "String"
                )
                if names not in {("pg_catalog", "numeric"), ("text",)}:
                    raise SQLValidationError(
                        "compiled SQL uses an unsupported type cast"
                    )
        if parameter_numbers != set(range(1, 10)):
            raise SQLValidationError("compiled SQL parameter bindings are incomplete")
        if any(count != 0 for count in expected_columns.values()):
            raise SQLValidationError("compiled SQL omits required scope columns")


class QueryCompiler:
    """Compile validated intent with only fixed registered expression templates."""

    @staticmethod
    def compile(plan: ValidatedMetricPlan) -> CompiledMetricQuery:
        definition = plan.definition
        registered_expression = _EXPRESSION_SQL.get(definition.expression_key)
        allowed_columns = _SOURCE_COLUMNS.get(definition.source_view)
        if (
            registered_expression is None
            or allowed_columns is None
            or definition.aggregation != "SUM"
            or definition.time_column not in allowed_columns
            or any(item not in allowed_columns for item in plan.dimensions)
        ):
            raise QueryPlanError("registered metric definition has no safe template")

        expression, aggregation_label = registered_expression
        dimension_sql = ", ".join(plan.dimensions)
        dimension_projection = f"{dimension_sql},\n    " if dimension_sql else ""
        group_clause = f"GROUP BY {dimension_sql}\n" if dimension_sql else ""
        sql = f"""
SELECT
    {dimension_projection}COALESCE({expression} FILTER (
        WHERE {definition.time_column} >= $5 AND {definition.time_column} < $6
    ), 0::numeric) AS current_value,
    COALESCE({expression} FILTER (
        WHERE {definition.time_column} >= $7 AND {definition.time_column} < $8
    ), 0::numeric) AS previous_value,
    COUNT(*) FILTER (
        WHERE {definition.time_column} >= $5 AND {definition.time_column} < $6
    ) AS current_row_count,
    COUNT(*) FILTER (
        WHERE {definition.time_column} >= $7 AND {definition.time_column} < $8
    ) AS previous_row_count
FROM {definition.source_view}
WHERE data_snapshot_id = $1
  AND region_code = ANY($2::text[])
  AND {definition.time_column} >= $3
  AND {definition.time_column} < $4
{group_clause}LIMIT $9
"""
        parameters = (
            plan.snapshot_id,
            list(plan.regions),
            plan.previous_start,
            plan.current_end,
            plan.current_start,
            plan.current_end,
            plan.previous_start,
            plan.previous_end,
            plan.limit + 1,
        )
        SQLValidator.validate(sql, definition, plan.dimensions)
        query_plan = {
            "metric": definition.metric_key,
            "metric_version": definition.version,
            "unit": definition.unit,
            "source_view": definition.source_view,
            "aggregation": aggregation_label,
            "time_column": definition.time_column,
            "dimensions": list(plan.dimensions),
            "dimension_results": [],
            "regions": list(plan.regions),
            "data_snapshot_id": plan.snapshot_id,
            "current_period": {
                "start": plan.current_start.isoformat(),
                "end_exclusive": plan.current_end.isoformat(),
            },
            "previous_period": {
                "start": plan.previous_start.isoformat(),
                "end_exclusive": plan.previous_end.isoformat(),
            },
            "comparison": plan.comparison,
            "limit": plan.limit,
            "parameters": [
                "data_snapshot_id", "region_code", "previous_start",
                "current_end_exclusive", "current_start", "current_end_exclusive",
                "previous_start", "previous_end_exclusive", "limit",
            ],
            "annotations": [],
        }
        return CompiledMetricQuery(sql, parameters, query_plan)


class ResultNormalizer:
    """Convert PostgreSQL aggregate values into stable Evidence result fields."""

    @staticmethod
    def normalize(row: Any) -> MetricResult:
        if isinstance(row, (list, tuple)):
            rows = row
        elif row is None:
            rows = ()
        else:
            rows = (row,)
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in rows:
            values = tuple(item.get(key) for key in item.keys()
                           if key not in {
                               "current_value", "previous_value",
                               "current_row_count", "previous_row_count",
                           })
            current = _money(item["current_value"])
            previous = _money(item["previous_value"])
            current_count = int(item["current_row_count"])
            previous_count = int(item["previous_row_count"])
            groups[values] = {
                "current_value": current,
                "previous_value": previous,
                "current_row_count": current_count,
                "previous_row_count": previous_count,
            }
        if not groups:
            groups[()] = {
                "current_value": Decimal("0.00"),
                "previous_value": Decimal("0.00"),
                "current_row_count": 0,
                "previous_row_count": 0,
            }
        current_total = sum(
            (group["current_value"] for group in groups.values()), Decimal("0.00")
        )
        previous_total = sum(
            (group["previous_value"] for group in groups.values()), Decimal("0.00")
        )
        current_count_total = sum(
            group["current_row_count"] for group in groups.values()
        )
        previous_count_total = sum(
            group["previous_row_count"] for group in groups.values()
        )
        delta = calculate_gmv_delta(
            current_value=current_total,
            previous_value=previous_total,
            current_row_count=current_count_total,
            previous_row_count=previous_count_total,
        )
        dimension_results = []
        group_columns = tuple(
            key for key in rows[0].keys()
            if key not in {
                "current_value", "previous_value",
                "current_row_count", "previous_row_count",
            }
        ) if rows else ()
        for values, group in sorted(groups.items(), key=lambda pair: pair[0]):
            part = calculate_gmv_delta(
                current_value=group["current_value"],
                previous_value=group["previous_value"],
                current_row_count=group["current_row_count"],
                previous_row_count=group["previous_row_count"],
            )
            dimension_results.append({
                "dimensions": dict(zip(group_columns, values, strict=True)),
                "current_value": f"{part.current_value:.2f}",
                "previous_value": f"{part.previous_value:.2f}",
                "absolute_change": f"{part.absolute_change:.2f}",
                "change_rate": (
                    None if part.change_rate is None else f"{part.change_rate:.4f}"
                ),
                "row_count": part.current_row_count,
                "annotations": list(part.annotations),
            })
        return MetricResult(
            current_value=delta.current_value,
            previous_value=delta.previous_value,
            absolute_change=delta.absolute_change,
            change_rate=delta.change_rate,
            row_count=delta.current_row_count,
            previous_row_count=delta.previous_row_count,
            dimension_results=tuple(dimension_results),
            annotations=delta.annotations,
        )


def compile_metric_query(
    plan: dict[str, Any],
    *,
    definition: MetricDefinition | None,
    region_scope: tuple[str, ...],
    data_snapshot_id: str,
) -> CompiledMetricQuery:
    intent = MetricPlanner.plan(plan)
    validated = SemanticValidator.validate(
        intent,
        definition,
        region_scope=region_scope,
        data_snapshot_id=data_snapshot_id,
    )
    return QueryCompiler.compile(validated)


def _money(value: Any) -> Decimal:
    return Decimal(value).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_gmv_delta(
    *,
    current_value: Any,
    previous_value: Any,
    current_row_count: int,
    previous_row_count: int,
) -> GmvDelta:
    """Compute change deterministically with Decimal and no zero division."""
    current = _money(current_value)
    previous = _money(previous_value)
    absolute = _money(current - previous)
    annotations: list[str] = []
    if previous == 0:
        change_rate = None
        annotations.append(ZERO_BASELINE)
    else:
        change_rate = ((current - previous) / previous).quantize(
            _RATE_QUANTUM, rounding=ROUND_HALF_UP
        )
    if current_row_count == 0:
        annotations.append(DATA_INSUFFICIENT)
    return GmvDelta(
        current_value=current,
        previous_value=previous,
        absolute_change=absolute,
        change_rate=change_rate,
        current_row_count=current_row_count,
        previous_row_count=previous_row_count,
        annotations=tuple(annotations),
    )
