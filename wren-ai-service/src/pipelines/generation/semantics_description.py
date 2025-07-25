import logging
import sys
from typing import Any

import orjson
from hamilton import base
from hamilton.async_driver import AsyncDriver
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe
from pydantic import BaseModel

from src.core.pipeline import BasicPipeline
from src.core.provider import LLMProvider
from src.pipelines.common import clean_up_new_lines
from src.utils import trace_cost

logger = logging.getLogger("wren-ai-service")


## Start of Pipeline
@observe(capture_input=False)
def picked_models(mdl: dict, selected_models: list[str]) -> list[dict]:
    def relation_filter(column: dict) -> bool:
        return "relationship" not in column

    def column_formatter(columns: list[dict]) -> list[dict]:
        return [
            {
                "name": column["name"],
                "type": column["type"],
                "properties": {
                    "description": column["properties"].get("description", ""),
                    "alias": column["properties"].get("displayName", ""),
                },
            }
            for column in columns
            if relation_filter(column)
        ]

    def extract(model: dict) -> dict:
        return {
            "name": model["name"],
            "columns": column_formatter(model["columns"]),
            "properties": {
                "description": model["properties"].get("description", ""),
                "alias": model["properties"].get("displayName", ""),
            },
        }

    return [
        extract(model)
        for model in mdl.get("models", [])
        if model.get("name", "") in selected_models
    ]


@observe(capture_input=False)
def prompt(
    picked_models: list[dict],
    user_prompt: str,
    prompt_builder: PromptBuilder,
    language: str,
) -> dict:
    _prompt = prompt_builder.run(
        picked_models=picked_models,
        user_prompt=user_prompt,
        language=language,
    )
    return {"prompt": clean_up_new_lines(_prompt.get("prompt"))}


@observe(as_type="generation", capture_input=False)
@trace_cost
async def generate(prompt: dict, generator: Any, generator_name: str) -> dict:
    return await generator(prompt=prompt.get("prompt")), generator_name


@observe(capture_input=False)
def normalize(generate: dict) -> dict:
    def wrapper(text: str) -> str:
        text = text.replace("\n", " ")
        text = " ".join(text.split())
        # Convert the normalized text to a dictionary
        try:
            text_dict = orjson.loads(text.strip())
            return text_dict
        except orjson.JSONDecodeError as e:
            logger.error(f"Error decoding JSON: {e}")
            return {"models": []}  # Return an empty list if JSON decoding fails

    reply = generate.get("replies")[0]  # Expecting only one reply
    normalized = wrapper(reply)

    return {model["name"]: model for model in normalized["models"]}


@observe(capture_input=False)
def output(normalize: dict, picked_models: list[dict]) -> dict:
    def _filter(enriched: list[dict], columns: list[dict]) -> list[dict]:
        valid_columns = [col["name"] for col in columns]

        return [col for col in enriched if col["name"] in valid_columns]

    models = {model["name"]: model for model in picked_models}

    return {
        name: {**data, "columns": _filter(data["columns"], models[name]["columns"])}
        for name, data in normalize.items()
        if name in models
    }


## End of Pipeline
class ModelProperties(BaseModel):
    description: str


class ModelColumns(BaseModel):
    name: str
    properties: ModelProperties


class SemanticModel(BaseModel):
    name: str
    columns: list[ModelColumns]
    properties: ModelProperties


class SemanticResult(BaseModel):
    models: list[SemanticModel]


SEMANTICS_DESCRIPTION_MODEL_KWARGS = {
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "semantic_description",
            "schema": SemanticResult.model_json_schema(),
        },
    }
}

system_prompt = """
I have a data model represented in JSON format, with the following structure:

```
[
    {'name': 'model', 'columns': [
            {'name': 'column_1', 'type': 'type', 'properties': {}
            },
            {'name': 'column_2', 'type': 'type', 'properties': {}
            },
            {'name': 'column_3', 'type': 'type', 'properties': {}
            }
        ], 'properties': {}
    }
]
```

Your task is to update this JSON structure by adding a `description` field inside both the `properties` attribute of each `column` and the `model` itself.
Each `description` should be derived from a user-provided input that explains the purpose or context of the `model` and its respective columns.
Follow these steps:
1. **For the `model`**: Prompt the user to provide a brief description of the model's overall purpose or its context. Insert this description in the `properties` field of the `model`.
2. **For each `column`**: Ask the user to describe each column's role or significance. Each column's description should be added under its respective `properties` field in the format: `'description': 'user-provided text'`.
3. Ensure that the output is a well-formatted JSON structure, preserving the input's original format and adding the appropriate `description` fields.

### Output Format:

```
{
    "models": [
        {
        "name": "model",
        "columns": [
            {
                "name": "column_1",
                "properties": {
                    "description": "<description for column_1>"
                }
            },
            {
                "name": "column_2",
                "properties": {
                    "description": "<description for column_1>"
                }
            },
            {
                "name": "column_3",
                "properties": {
                    "description": "<description for column_1>"
                }
            }
        ],
        "properties": {
                "description": "<description for model>"
            }
        }
    ]
}
```

Make sure that the descriptions are concise, informative, and contextually appropriate based on the input provided by the user.
"""

user_prompt_template = """
### Input:
User's prompt: {{ user_prompt }}
Picked models: {{ picked_models }}
Localization Language: {{ language }}

Please provide a brief description for the model and each column based on the user's prompt.
"""


class SemanticsDescription(BasicPipeline):
    def __init__(self, llm_provider: LLMProvider, **_):
        self._components = {
            "prompt_builder": PromptBuilder(template=user_prompt_template),
            "generator": llm_provider.get_generator(
                system_prompt=system_prompt,
                generation_kwargs=SEMANTICS_DESCRIPTION_MODEL_KWARGS,
            ),
            "generator_name": llm_provider.get_model(),
        }
        self._final = "output"

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    @observe(name="Semantics Description Generation")
    async def run(
        self,
        user_prompt: str,
        selected_models: list[str],
        mdl: dict,
        language: str = "en",
    ) -> dict:
        logger.info("Semantics Description Generation pipeline is running...")
        return await self._pipe.execute(
            [self._final],
            inputs={
                "user_prompt": user_prompt,
                "selected_models": selected_models,
                "mdl": mdl,
                "language": language,
                **self._components,
            },
        )
