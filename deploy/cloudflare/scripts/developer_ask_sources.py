#!/usr/bin/env python3
"""Stage the original Developer ask wire contract, context and default RAG prompt."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes, source


def method(relative: str, owner: str, name: str) -> str:
    original = source(relative)
    cls = next(node for node in ast.parse(original).body if isinstance(node, ast.ClassDef) and node.name == owner)
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    start = min(node.lineno, *(item.lineno for item in node.decorator_list))
    return '\n'.join(original.splitlines()[start - 1 : node.end_lineno]) + '\n'


def generate(output: Path) -> None:
    contract = (
        'from __future__ import annotations\n'
        'from datetime import datetime\nfrom typing import List, Optional\n'
        'from zoneinfo import ZoneInfoNotFoundError\n'
        'from worker_timezone import load_zoneinfo as ZoneInfo\n'
        'from pydantic import BaseModel, Field, field_validator\n\n'
        + selected_nodes(
            'backend/routers/developer.py',
            {
                'DeveloperAskRequest',
                'DeveloperAskSource',
                'DeveloperAskResponse',
                '_ASK_NO_CONTEXT',
                '_ask_context_from_conversations',
            },
        )
    )
    prompt = (
        'from __future__ import annotations\n'
        'from datetime import datetime, timezone\nfrom types import FunctionType\n'
        'from typing import Any, Dict, List, Optional, Sequence\n\nclass Message:\n'
        + method('backend/models/chat.py', 'Message', 'get_messages_as_xml')
        + '\nclass Memory:\n'
        + method('backend/models/memories.py', 'Memory', 'get_memories_as_str')
        + '\n'
        + selected_nodes('backend/utils/llms/memory.py', {'PROMPT_MEMORY_LIMIT', '_render_legacy_prompt_context'})
        + '\n'
        + selected_nodes('backend/utils/llm/chat.py', {'_get_qa_rag_prompt'})
        + '\n\ndef render_prompt(uid, question, context, facts, tz):\n'
        '    # Give this invocation its own data adapter; never mutate shared globals or the prompt.\n'
        '    render = FunctionType(_get_qa_rag_prompt.__code__,\n'
        '        {**globals(), "get_prompt_memories": lambda owner: (None, facts)},\n'
        '        argdefs=_get_qa_rag_prompt.__defaults__)\n'
        '    return render(uid, question, context, cited=True, tz=tz)\n'
    )
    outputs = {'developer_ask_contract.py': contract, 'developer_ask_prompt.py': prompt}
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('Developer ask source collides with an existing module')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
