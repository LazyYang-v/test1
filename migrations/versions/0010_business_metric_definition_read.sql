-- 0010: permit only the read-only metrics role to discover registered metric
-- semantics. Business rows remain accessible through the metric view only.
GRANT SELECT (
    metric_key,
    version,
    source_view,
    expression_key,
    allowed_dimensions,
    allowed_filters,
    aggregation,
    unit,
    time_column,
    status
)
ON business.metric_definitions TO agentforge_business_ro;
