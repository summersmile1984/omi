#!/usr/bin/env python3
"""Stage the upstream feedback wire types and pure report policy for Core."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes, source


def generate(output: Path) -> None:
    outputs = {
        'feedback_contract.py': source('backend/models/feedback.py'),
        'feedback_desktop_contract.py': (
            'from pydantic import BaseModel, Field\n'
            'from feedback_contract import FeedbackReason, FeedbackSurface, MAX_COMMENT_LENGTH\n\n'
            + selected_nodes('backend/routers/chat_sessions.py', {'RateMessageRequest', '_LEDGER_SURFACES'})
        ),
        'feedback_policy.py': (
            'from __future__ import annotations\nimport json\n'
            'from datetime import date as date_cls, datetime, time, timedelta, timezone\n'
            'from typing import Optional\n'
            'from feedback_contract import FeedbackEvent, FeedbackReportEntry\n\n'
            + selected_nodes(
                'backend/database/feedback.py',
                {'MAX_REPORT_ENTRIES', 'RAW_FETCH_LIMIT', 'MAX_REPORT_DOCUMENT_BYTES'},
            )
            + '\n'
            + selected_nodes(
                'backend/utils/feedback_context.py',
                {'FOLLOW_UP_WINDOW_SECONDS', 'MAX_PRECEDING_TURNS', 'MAX_FOLLOW_UP_TURNS', 'MAX_HYDRATED_TEXT_CHARS'},
            )
            + '\n'
            + selected_nodes(
                'backend/jobs/feedback_daily_report.py',
                {'_day_bounds', 'previous_utc_day', '_is_more_informative', '_collapse_per_target', '_entry_bytes'},
            )
        ),
    }
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('feedback projection collides with a staged source owner')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
