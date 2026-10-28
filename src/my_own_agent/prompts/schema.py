"""Emit the implemented inspection schema: uv run python -m my_own_agent.prompts.schema."""

import json

from my_own_agent.contracts.v1.prompt import PromptInspection

if __name__ == "__main__":
    print(json.dumps(PromptInspection.model_json_schema(), ensure_ascii=False, indent=2))
