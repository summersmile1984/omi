#!/usr/bin/env python3
"""Stage upstream calendar recipient rules and their public response contract."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes


def generate(output: Path) -> None:
    content = (
        'from __future__ import annotations\n'
        'import re\nfrom typing import Any, Dict, List, Optional\n'
        'from pydantic import BaseModel\n\n'
        + selected_nodes(
            'backend/utils/conversations/share_email.py',
            {
                'MAX_MEETING_PARTICIPANTS',
                'MAX_RECIPIENTS',
                'CALENDAR_BACKED_SOURCES',
                '_EMAIL_RE',
                '_normalized_email',
                'normalized_recipient_emails',
                '_participants_from_conversation',
                '_attendee_count',
                '_is_captured_name',
                'extract_share_recipients',
            },
        )
        + '\n'
        + selected_nodes('backend/routers/conversations.py', {'ShareRecipient', 'ShareRecipientsResponse'})
    )
    target = output / 'share_recipient_contract.py'
    if target.exists():
        raise ValueError('share recipient contract has a competing source owner')
    target.write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
