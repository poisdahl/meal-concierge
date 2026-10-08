"""Report advertised MCP catalog size; this is not model-context telemetry.

Run offline with the pinned runtime: python -I -B tests/measure_agent_catalog.py
"""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def catalog_metrics(tools):
    """Use an explicit serialization so repeated revisions are comparable."""
    documents = [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]
    return {
        'tool_count': len(documents),
        'advertised_catalog_chars': len(json.dumps(documents, ensure_ascii=True)),
        'serialization': 'JSON array; by_alias=True; exclude_none=True; ensure_ascii=True; default separators',
        'observed_model_context_tokens': None,
        'context_measurement': 'unknown: server advertisement does not establish host prompt exposure',
    }


def measure():
    from mcp_server import server
    return catalog_metrics(asyncio.run(server.list_tools()))


if __name__ == '__main__':
    print(json.dumps(measure(), indent=2))
