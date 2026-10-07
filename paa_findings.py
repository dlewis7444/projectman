"""GTK-free finding text shared by the card window and the Telegram bot.

The Discuss prompt matches the card window's historical wording, including
the sibling block, so a phone discuss and a desktop discuss ask the same
question.
"""
from __future__ import annotations


TYPE_LABELS = {
    'missing-agents-md': 'Missing AGENTS.md',
    'context-drift': 'Context Drift',
    'no-git': 'No Git Repo',
    'ai-semantic-staleness': 'Semantic Staleness',
    'ai-dependency-outdated': 'Outdated Dependency',
    'ai-health-concern': 'Health Concern',
    'xp-dep-conflict': 'Dep Conflict',
    'xp-broken-reference': 'Broken Reference',
    'xp-stale-project': 'Stale Project',
}

FINDING_PAGE_SIZE = 8


def finding_notice_text(item) -> str:
    """One short line: project, severity, summary."""
    return f'{item.project} [{item.severity}] {item.summary}'


def discuss_finding_prompt(item, pending_items) -> str:
    """The ``DISCUSS FINDING`` prompt used by the card window and the bot.

    ``pending_items`` is the current pending list (siblings are the other
    pending rows for the same project).
    """
    siblings = [
        i for i in pending_items
        if getattr(i, 'project', None) == item.project and i.id != item.id
    ]
    sibling_block = ''
    if siblings:
        lines = []
        for s in siblings:
            lbl = TYPE_LABELS.get(s.type, s.type)
            lines.append(f'  - [{s.severity}] {lbl}: {s.summary}')
        sibling_block = (
            f'\n\nOTHER PENDING FINDINGS FOR THIS PROJECT '
            f'({len(siblings)}):\n'
            + '\n'.join(lines)
            + '\n\nThe user may want to address some of these together. '
              'Focus on the primary finding above unless asked.'
        )
    return (
        f'DISCUSS FINDING\n\n'
        f'Type: {item.type}\n'
        f'Project: {item.project}\n'
        f'Severity: {item.severity}\n'
        f'Summary: {item.summary}\n'
        f'Evidence: {item.evidence}\n\n'
        f'Please help me understand this finding and suggest how to address it. '
        f'The project is at ../{item.project}/ relative to your working directory.'
        f'{sibling_block}'
    )


def findings_page_text(items, page: int, page_size: int = FINDING_PAGE_SIZE) -> str:
    """Pending list text for one page. Page size defaults to 8."""
    if page < 0:
        page = 0
    start = page * page_size
    chunk = list(items)[start:start + page_size]
    if not chunk:
        return 'No pending findings.'
    lines = [f'Pending findings ({start + 1}-{start + len(chunk)} of {len(items)}):']
    for item in chunk:
        lines.append(finding_notice_text(item))
    return '\n'.join(lines)
