"""Class to describe a Glean Ping View."""
import logging
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Union

import click
from mozilla_schema_generator.glean_ping import GleanPing
from mozilla_schema_generator.probes import GleanProbe

from .ping_view import PingView

DISTRIBUTION_TYPES = {
    "timing_distribution",
    "memory_distribution",
    "custom_distribution",
}


ALLOWED_TYPES = DISTRIBUTION_TYPES | {
    "boolean",
    "counter",
    "datetime",
    "jwe",
    "quantity",
    "string",
    "rate",
    "timespan",
    "uuid",
}


class GleanPingView(PingView):
    """A view on a ping table for an application using the Glean SDK."""

    type: str = "glean_ping_view"
    allow_glean: bool = True

    def _get_links(self, dimension: dict) -> List[Dict[str, str]]:
        """Get a link annotation given a metric name."""
        name = self._get_name(dimension)
        title = name.replace("_", " ").title()
        return [
            {
                "label": (f"Glean Dictionary reference for {title}"),
                "url": (
                    f"https://dictionary.telemetry.mozilla.org"
                    f"/apps/{self.namespace}/metrics/{name}"
                ),
                "icon_url": "https://dictionary.telemetry.mozilla.org/favicon.png",
            }
        ]

    def _get_name(self, dimension: dict) -> str:
        return dimension["name"].split("__")[-1]

    def _get_metric_type(self, dimension: dict) -> str:
        return dimension["name"].split("__")[1]

    def _is_metric(self, dimension) -> bool:
        return dimension["name"].startswith("metrics__")

    def _get_glean_metrics(self, v1_name: Optional[str]) -> List[GleanProbe]:
        if v1_name is None:
            logging.error(
                f"Error: Missing v1 name for ping {self.name} in namespace {self.namespace}"
            )
            return []

        repo = next((r for r in GleanPing.get_repos() if r["name"] == v1_name))
        glean_app = GleanPing(repo)

        ping_probes = []
        probe_ids = set()
        for probe in glean_app.get_probes():
            if self.name not in probe.definition["send_in_pings"]:
                continue
            if probe.id in probe_ids:
                # Some ids are duplicated, ignore them
                continue

            ping_probes.append(probe)
            probe_ids.add(probe.id)

        return ping_probes

    def _make_dimension(
        self, metric: GleanProbe, suffix: str, sql_map: Dict[str, Dict[str, str]]
    ) -> Optional[Dict[str, Union[str, List[Dict[str, str]]]]]:
        *category, name = metric.id.split(".")
        category = "_".join(category)

        label = name
        sep = "_"
        if not category:
            sep = ""
        looker_name = f"metrics__{metric.type}__{category}{sep}{name}"
        if suffix:
            label = f"{name}_{suffix}"
            looker_name = f"metrics__{metric.type}__{category}{sep}{name}__{suffix}"

        if looker_name not in sql_map:
            return None

        group_label = category.replace("_", " ").title()
        group_item_label = label.replace("_", " ").title()

        if not group_label:
            group_label = "Glean"

        lookml = {
            "name": looker_name,
            "sql": sql_map[looker_name]["sql"],
            "type": sql_map[looker_name]["type"],
            "group_label": group_label,
            "group_item_label": group_item_label,
            "links": [
                {
                    "label": (
                        f"Glean Dictionary reference for {group_label} {group_item_label}"
                    ),
                    "url": (
                        f"https://dictionary.telemetry.mozilla.org"
                        f"/apps/{self.namespace}/metrics/{category}{sep}{name}"
                    ),
                    "icon_url": "https://dictionary.telemetry.mozilla.org/favicon.png",
                },
            ],
        }

        if metric.description:
            lookml["description"] = metric.description

        return lookml

    def _get_metric_dimensions(
        self, metric: GleanProbe, sql_map: Dict[str, Dict[str, str]]
    ) -> Iterable[Optional[Dict[str, Union[str, List[Dict[str, str]]]]]]:
        if metric.type == "rate":
            for suffix in ("numerator", "denominator"):
                yield self._make_dimension(metric, suffix, sql_map)
        elif metric.type in DISTRIBUTION_TYPES:
            yield self._make_dimension(metric, "sum", sql_map)
        elif metric.type == "timespan":
            yield self._make_dimension(metric, "value", sql_map)
        elif metric.type in ALLOWED_TYPES:
            yield self._make_dimension(metric, "", sql_map)

    def _get_glean_metric_dimensions(
        self, all_fields: List[dict], v1_name: Optional[str]
    ):
        sql_map = {
            f["name"]: {"sql": f["sql"], "type": f.get("type", "string")}
            for f in all_fields
        }
        metrics = self._get_glean_metrics(v1_name)
        return [
            dimension
            for metric in metrics
            for dimension in self._get_metric_dimensions(metric, sql_map)
            if dimension is not None
        ]

    def _add_link(self, dimension):
        annotations = {}
        if self._is_metric(dimension) and not self._get_metric_type(
            dimension
        ).startswith("labeled"):
            annotations["links"] = self._get_links(dimension)

        return dict(dimension, **annotations)

    def get_dimensions(
        self, bq_client, table, v1_name: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Get the set of dimensions for this view."""
        all_fields = super().get_dimensions(bq_client, table, v1_name)
        return self._get_glean_metric_dimensions(all_fields, v1_name) + [
            self._add_link(d)
            for d in all_fields
            if not d["name"].startswith("metrics__")
        ]

    def get_measures(
        self, dimensions: List[dict], table: str, v1_name: Optional[str]
    ) -> List[Dict[str, Union[str, List[Dict[str, str]]]]]:
        """Generate measures from a list of dimensions.

        When no dimension-specific measures are found, return a single "count" measure.

        Raise ClickException if dimensions result in duplicate measures.
        """
        measures = super().get_measures(dimensions, table, v1_name)
        client_id_field = self._get_client_id(dimensions, table)

        for dimension in dimensions:
            if (
                self._is_metric(dimension)
                and self._get_metric_type(dimension) == "counter"
            ):
                # handle the counters in the metric ping
                name = self._get_name(dimension)
                dimension_name = dimension["name"]
                measures += [
                    {
                        "name": name,
                        "type": "sum",
                        "sql": f"${{{dimension_name}}}",
                        "links": self._get_links(dimension),
                    },
                    {
                        "name": f"{name}_client_count",
                        "type": "count_distinct",
                        "filters": [{dimension_name: ">0"}],
                        "sql": f"${{{client_id_field}}}",
                        "links": self._get_links(dimension),
                    },
                ]

        # check if there are any duplicate values
        names = [measure["name"] for measure in measures]
        duplicates = [k for k, v in Counter(names).items() if v > 1]
        if duplicates:
            raise click.ClickException(
                f"duplicate measures {duplicates!r} for table {table!r}"
            )

        return measures
