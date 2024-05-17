import json
import logging
import os
from typing import Any, Dict, List

import orjson
from haystack import Document, Pipeline, component
from haystack.components.writers import DocumentWriter
from haystack.document_stores.types import DocumentStore, DuplicatePolicy
from tqdm import tqdm

from src.core.provider import LLMProvider
from src.core.provider import DocumentStoreProvider
from src.core.pipeline import BasicPipeline
from src.utils import init_providers, load_env_vars

load_env_vars()
logger = logging.getLogger("wren-ai-service")

DATASET_NAME = os.getenv("DATASET_NAME")


@component
class DocumentCleaner:
    """
    This component is used to clear all the documents in the specified document store(s).

    """

    def __init__(self, stores: List[DocumentStore]) -> None:
        self._stores = stores

    @component.output_types(mdl=str)
    def run(self, mdl: str) -> str:
        def _clear_documents(store: DocumentStore) -> None:
            ids = [str(i) for i in range(store.count_documents())]
            if ids:
                store.delete_documents(ids)

        logger.info("Ask Indexing pipeline is clearing old documents...")
        [_clear_documents(store) for store in self._stores]
        return {"mdl": mdl}


@component
class MDLValidator:
    """
    Validate the MDL to check if it is a valid JSON and contains the required keys.
    """

    @component.output_types(mdl=Dict[str, Any])
    def run(self, mdl: str) -> str:
        try:
            mdl_json = orjson.loads(mdl)
            logger.debug(f"MDL JSON: {mdl_json}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        if "models" not in mdl_json:
            mdl_json["models"] = []
        if "views" not in mdl_json:
            mdl_json["views"] = []
        if "relationships" not in mdl_json:
            mdl_json["relationships"] = []
        if "metrics" not in mdl_json:
            mdl_json["metrics"] = []

        return {"mdl": mdl_json}


@component
class ViewConverter:
    """
    Convert the view MDL to the following format:
    {
      "question":"user original query",
      "description":"the description generated by LLM",
      "statement":"the SQL statement generated by LLM"
    }
    and store it in the view store.
    """

    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any]) -> None:
        def _format(view: Dict[str, Any]) -> List[str]:
            return str(
                {
                    "question": (
                        view["properties"]["question"]
                        if "properties" in view and "question" in view["properties"]
                        else ""
                    ),
                    "description": (
                        view["properties"]["description"]
                        if "properties" in view and "description" in view["properties"]
                        else ""
                    ),
                    "statement": view["statement"],
                }
            )

        converted_views = (
            [_format(view) for view in mdl["views"]] if mdl["views"] else []
        )

        return {
            "documents": [
                Document(
                    id=str(i),
                    meta={"id": str(i)},
                    content=converted_view,
                )
                for i, converted_view in enumerate(
                    tqdm(
                        converted_views,
                        desc="indexing view into the historial view question store",
                    )
                )
            ]
        }


@component
class DDLConverter:
    @component.output_types(documents=List[Document])
    def run(self, mdl: Dict[str, Any]):
        logger.info("Ask Indexing pipeline is writing new documents...")

        logger.debug(f"original mdl_json: {mdl}")

        semantics = {
            "models": [],
            "relationships": mdl["relationships"],
            "views": mdl["views"],
            "metrics": mdl["metrics"],
        }

        for model in mdl["models"]:
            columns = []
            for column in model["columns"]:
                ddl_column = {
                    "name": column["name"],
                    "type": column["type"],
                }
                if "properties" in column:
                    ddl_column["properties"] = column["properties"]
                if "relationship" in column:
                    ddl_column["relationship"] = column["relationship"]
                if "expression" in column:
                    ddl_column["expression"] = column["expression"]
                if "isCalculated" in column:
                    ddl_column["isCalculated"] = column["isCalculated"]

                columns.append(ddl_column)

            semantics["models"].append(
                {
                    "type": "model",
                    "name": model["name"],
                    "properties": model["properties"] if "properties" in model else {},
                    "columns": columns,
                    "primaryKey": model["primaryKey"],
                }
            )

        ddl_commands = (
            self._convert_models_and_relationships(
                semantics["models"], semantics["relationships"]
            )
            + self._convert_metrics(semantics["metrics"])
            + self._convert_views(semantics["views"])
        )

        return {
            "documents": [
                Document(
                    id=str(i),
                    meta={"id": str(i)},
                    content=ddl_command,
                )
                for i, ddl_command in enumerate(
                    tqdm(
                        ddl_commands,
                        desc="indexing ddl commands into the ddl store",
                    )
                )
            ]
        }

    # TODO: refactor this method
    def _convert_models_and_relationships(
        self, models: List[Dict[str, Any]], relationships: List[Dict[str, Any]]
    ) -> List[str]:
        ddl_commands = []

        # A map to store model primary keys for foreign key relationships
        primary_keys_map = {model["name"]: model["primaryKey"] for model in models}

        for model in models:
            table_name = model["name"]
            columns_ddl = []
            for column in model["columns"]:
                if "relationship" not in column:
                    if "properties" in column:
                        comment = f"-- {orjson.dumps(column['properties']).decode("utf-8")}\n  "
                    else:
                        comment = ""
                    if "isCalculated" in column and column["isCalculated"]:
                        comment = (
                            comment
                            + f"-- This column is a Calculated Field\n  -- column expression: {column["expression"]}\n  "
                        )
                    column_name = column["name"]
                    column_type = column["type"]
                    column_ddl = f"{comment}{column_name} {column_type}"

                    # If column is a primary key
                    if column_name == model.get("primaryKey", ""):
                        column_ddl += " PRIMARY KEY"

                    columns_ddl.append(column_ddl)

            # Add foreign key constraints based on relationships
            for relationship in relationships:
                if (
                    table_name == relationship["models"][0]
                    and relationship["joinType"].upper() == "MANY_TO_ONE"
                ):
                    related_table = relationship["models"][1]
                    fk_column = relationship["condition"].split(" = ")[0].split(".")[1]
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(fk_constraint)
                elif (
                    table_name == relationship["models"][1]
                    and relationship["joinType"].upper() == "ONE_TO_MANY"
                ):
                    related_table = relationship["models"][0]
                    fk_column = relationship["condition"].split(" = ")[1].split(".")[1]
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(fk_constraint)
                elif (
                    table_name in relationship["models"]
                    and relationship["joinType"].upper() == "ONE_TO_ONE"
                ):
                    index = relationship["models"].index(table_name)
                    related_table = [
                        m for m in relationship["models"] if m != table_name
                    ][0]
                    fk_column = (
                        relationship["condition"].split(" = ")[index].split(".")[1]
                    )
                    fk_constraint = f"FOREIGN KEY ({fk_column}) REFERENCES {related_table}({primary_keys_map[related_table]})"
                    columns_ddl.append(fk_constraint)

            if "properties" in model:
                comment = (
                    f"\n/* {orjson.dumps(model['properties']).decode("utf-8")} */\n"
                )
            else:
                comment = ""

            create_table_ddl = (
                f"{comment}CREATE TABLE {table_name} (\n  "
                + ",\n  ".join(columns_ddl)
                + "\n);"
            )
            ddl_commands.append(create_table_ddl)

        return ddl_commands

    def _convert_views(self, views: List[Dict[str, Any]]) -> List[str]:
        def _format(view: Dict[str, Any]) -> str:
            properties = view["properties"] if "properties" in view else ""
            return f"/* {properties} */\nCREATE VIEW {view['name']}\nAS ({view['statement']})"

        return [_format(view) for view in views]

    def _convert_metrics(self, metrics: List[Dict[str, Any]]) -> List[str]:
        ddl_commands = []

        for metric in metrics:
            table_name = metric["name"]
            columns_ddl = []
            for dimension in metric["dimension"]:
                column_name = dimension["name"]
                column_type = dimension["type"]
                comment = "-- This column is a dimension\n  "
                column_ddl = f"{comment}{column_name} {column_type}"
                columns_ddl.append(column_ddl)

            for measure in metric["measure"]:
                column_name = measure["name"]
                column_type = measure["type"]
                comment = f"-- This column is a measure\n  -- expression: {measure["expression"]}\n  "
                column_ddl = f"{comment}{column_name} {column_type}"
                columns_ddl.append(column_ddl)

            comment = f"\n/* This table is a metric */\n/* Metric Base Object: {metric["baseObject"]} */\n"
            create_table_ddl = (
                f"{comment}CREATE TABLE {table_name} (\n  "
                + ",\n  ".join(columns_ddl)
                + "\n);"
            )

            ddl_commands.append(create_table_ddl)

        return ddl_commands


class Indexing(BasicPipeline):
    def __init__(
        self, llm_provider: LLMProvider, store_provider: DocumentStoreProvider
    ) -> None:
        ddl_store = store_provider.get_store()
        view_store = store_provider.get_store(dataset_name="view_questions")

        pipe = Pipeline()
        pipe.add_component("validator", MDLValidator())
        pipe.add_component("cleaner", DocumentCleaner([ddl_store, view_store]))

        pipe.add_component("ddl_converter", DDLConverter())
        pipe.add_component("ddl_embedder", llm_provider.get_document_embedder())
        pipe.add_component(
            "ddl_writer",
            DocumentWriter(
                document_store=ddl_store,
                policy=DuplicatePolicy.OVERWRITE,
            ),
        )
        pipe.add_component("view_converter", ViewConverter())
        pipe.add_component("view_embedder", llm_provider.get_document_embedder())
        pipe.add_component(
            "view_writer",
            DocumentWriter(
                document_store=view_store,
                policy=DuplicatePolicy.OVERWRITE,
            ),
        )

        pipe.connect("cleaner", "validator")

        pipe.connect("validator", "ddl_converter")
        pipe.connect("ddl_converter", "ddl_embedder")
        pipe.connect("ddl_embedder", "ddl_writer")

        pipe.connect("validator", "view_converter")
        pipe.connect("view_converter", "view_embedder")
        pipe.connect("view_embedder", "view_writer")

        self._pipeline = pipe

        super().__init__(self._pipeline)

    def run(self, mdl_str: str) -> Dict[str, Any]:
        return self._pipeline.run({"cleaner": {"mdl": mdl_str}})


if __name__ == "__main__":
    indexing_pipeline = Indexing(*init_providers())

    print("generating indexing_pipeline.jpg to outputs/pipelines/ask...")
    indexing_pipeline.draw("./outputs/pipelines/ask/indexing_pipeline.jpg")
