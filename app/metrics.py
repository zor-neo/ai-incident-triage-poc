"""Custom CloudWatch metrics (spec section 4.2).

Metric emission is best-effort: a metrics failure must never turn a successful
triage response into a 5xx.
"""

from __future__ import annotations

import boto3

from app.config import Settings
from app.logging_config import get_logger
from app.models import TriageMetrics


class MetricsEmitter:
    def __init__(self, settings: Settings, client=None) -> None:
        self._settings = settings
        self._logger = get_logger("app.metrics")
        self._enabled = settings.metrics_enabled
        self._namespace = settings.metrics_namespace
        self._client = client
        if self._enabled and self._client is None:
            self._client = boto3.client("cloudwatch", region_name=settings.aws_region)

    def _put(
        self, name: str, value: float, unit: str, dimension: str, dimension_value: str
    ) -> None:
        if not self._enabled or self._client is None:
            return
        try:
            self._client.put_metric_data(
                Namespace=self._namespace,
                MetricData=[
                    {
                        "MetricName": name,
                        "Value": value,
                        "Unit": unit,
                        "Dimensions": [{"Name": dimension, "Value": dimension_value}],
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - never break the request path
            self._logger.warning("metric_emit_failed", metric=name, error=str(exc))

    def emit_triage_success(self, metrics: TriageMetrics) -> None:
        env = self._settings.environment
        self._put("TriageSuccess", 1, "Count", "Environment", env)
        if metrics.ai_latency_ms is not None:
            self._put("BedrockLatencyMs", metrics.ai_latency_ms, "Milliseconds", "Environment", env)
        tokens = (metrics.input_tokens or 0) + (metrics.output_tokens or 0)
        if tokens:
            self._put("TokensUsed", tokens, "Count", "Environment", env)

    def emit_triage_fallback(self, reason: str) -> None:
        self._put("TriageFallback", 1, "Count", "Environment", self._settings.environment)
        self._put("TriageFallbackByReason", 1, "Count", "Reason", reason)
