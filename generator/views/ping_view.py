"""Class to describe a Ping View."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import filterfalse
from typing import Any, Dict, Iterator, List

import click

from . import lookml_utils
from .view import OMIT_VIEWS, View, ViewDict


class PingView(View):
    """A view on a ping table."""

    type: str = "ping_view"
    allow_glean: bool = False

    def __init__(self, name: str, tables: List[Dict[str, str]], **kwargs):
        """Create instance of a PingView."""
        super().__init__(name, self.__class__.type, tables, **kwargs)

    @classmethod
    def from_db_views(
        klass, name: str, is_glean: bool, channels: List[Dict[str, str]], db_views: dict
    ) -> Iterator[PingView]:
        """Get Looker views for a namespace."""
        if (klass.allow_glean and not is_glean) or (not klass.allow_glean and is_glean):
            return

        views = defaultdict(list)
        for channel in channels:
            dataset = channel["dataset"]

            for view_id, references in db_views[dataset].items():
                if view_id in OMIT_VIEWS:
                    continue

                table: Dict[str, str] = {"table": f"mozdata.{dataset}.{view_id}"}

                if channel.get("channel") is not None:
                    table["channel"] = channel["channel"]
                if len(references) != 1 or references[0][-2] != f"{dataset}_stable":
                    continue  # This view is only for ping tables

                views[view_id].append(table)

        for view_id, tables in views.items():
            yield klass(view_id, tables)

    @classmethod
    def from_dict(klass, name: str, _dict: ViewDict) -> PingView:
        """Get a view from a name and dict definition."""
        return klass(name, _dict["tables"])

    def to_lookml(self, bq_client) -> List[dict]:
        """Generate LookML for this view."""
        view_defn: Dict[str, Any] = {"name": self.name}

        # use schema for the table where channel=="release" or the first one
        table = next(
            (table for table in self.tables if table.get("channel") == "release"),
            self.tables[0],
        )["table"]

        dimensions = self.get_dimensions(bq_client, table)
        view_defn["dimensions"] = list(
            filterfalse(lookml_utils._is_dimension_group, dimensions)
        )
        view_defn["dimension_groups"] = list(
            filter(lookml_utils._is_dimension_group, dimensions)
        )

        # add measures
        view_defn["measures"] = self.get_measures(dimensions, table)

        # parameterize table name
        if len(self.tables) > 1:
            view_defn["parameters"] = [
                {
                    "name": "channel",
                    "type": "unquoted",
                    "allowed_values": [
                        {
                            "label": table["channel"].title(),
                            "value": table["table"],
                        }
                        for table in self.tables
                    ],
                }
            ]
            view_defn["sql_table_name"] = "`{% parameter channel %}`"
        else:
            view_defn["sql_table_name"] = f"`{table}`"

        return [view_defn]

    def get_dimensions(self, bq_client, table) -> List[Dict[str, Any]]:
        """Get the set of dimensions for this view."""
        # add dimensions and dimension groups
        return lookml_utils._generate_dimensions(bq_client, table)

    def get_measures(self, dimensions: List[dict], table: str) -> List[Dict[str, str]]:
        """Generate measures from a list of dimensions.

        When no dimension-specific measures are found, return a single "count" measure.

        Raise ClickException if dimensions result in duplicate measures.
        """
        measures = []
        # iterate through each of the dimensions and accumulate any measures
        # that we want to include in the view
        for dimension in dimensions:
            dimension_name = dimension["name"]
            if dimension_name in {"client_id", "client_info__client_id"}:
                measure = {
                    "name": "clients",
                    "type": "count_distinct",
                    "sql": f"${{{dimension_name}}}",
                }
            elif dimension_name == "document_id":
                measure = {"name": "ping_count", "type": "count"}
            else:
                continue
            measures.append(measure)

        # check if there are any duplicate values, and report the first one that
        # shows up
        names = [measure["name"] for measure in measures]
        duplicates = [k for k, v in Counter(names).items() if v > 1]
        if duplicates:
            name = duplicates[0]
            raise click.ClickException(
                f"duplicate measure {name!r} for table {table!r}"
            )

        if not measures:
            raise click.ClickException(
                f"Missing client_id and doc_id dimensions in {table!r}"
            )

        return measures
