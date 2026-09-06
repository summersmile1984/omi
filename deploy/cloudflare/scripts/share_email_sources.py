#!/usr/bin/env python3
"""Stage upstream calendar recipient rules and their public response contract."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes, source


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
    renderer = selected_nodes('backend/utils/conversations/share_email.py', {'build_summary_email'})
    footer = '<a href="https://omi.me" style="color:#888">Omi</a>'
    signature = '    share_url: str,\n'
    if renderer.count(footer) != 1 or renderer.count(signature) != 1:
        raise ValueError('upstream email branding seam changed')
    renderer = renderer.replace(
        footer, '<a href="{_escape_html(brand_url)}" style="color:#888">{_escape_html(brand_name)}</a>'
    ).replace(signature, signature + '    brand_name: str,\n    brand_url: str,\n')
    outputs = {
        'share_email_markdown.py': source('backend/utils/conversations/overview_markdown.py'),
        'share_email_contract.py': (
            'from __future__ import annotations\n'
            'from typing import Any, Dict, List\nfrom pydantic import BaseModel, Field\n'
            'import share_recipient_contract as share_email\n'
            'from share_recipient_contract import _normalized_email\n'
            'from share_email_markdown import overview_to_email_html\n\n'
            + selected_nodes(
                'backend/utils/conversations/share_email.py',
                {'DAILY_SEND_QUOTA', '_sender_display_name', '_escape_html'},
            )
            + selected_nodes('backend/database/conversations.py', {'SHARE_EMAIL_CLAIM_TTL_SECONDS'})
            + selected_nodes('backend/routers/conversations.py', {'SendShareEmailRequest', 'SendShareEmailResponse'})
            + renderer
        ),
    }
    for name, content in outputs.items():
        target = output / name
        if target.exists():
            raise ValueError('share email contract has a competing source owner')
        target.write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
