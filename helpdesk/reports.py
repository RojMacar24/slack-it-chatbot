"""Summary of the tickets this bot opened, read back from Jira so the numbers match what's really there."""

from collections import Counter
from urllib.parse import quote

from .assistant import CATEGORY_NAMES, KIND_NAMES
from .jira_client import is_done
from .tickets import CATEGORY_LABEL_PREFIX, ESCALATED_LABEL


def build_report(jira, project_key, label, days=7):
    """Slack-formatted summary of tickets created in the last `days` days."""
    jql = f'project = "{project_key}" AND labels = "{label}" AND created >= -{days}d ORDER BY created DESC'
    issues = [issue.get("fields") or {} for issue in jira.search(jql, fields=["status", "labels"])]
    title = f":bar_chart: *IT help desk: last {days} days*"
    if not issues:
        return f"{title}\nNo tickets were opened."

    kinds, categories = Counter(), Counter()
    for fields in issues:
        labels = set(fields.get("labels") or [])
        kinds.update(kind for kind in KIND_NAMES if kind in labels)
        categories.update(cat for cat in CATEGORY_NAMES if CATEGORY_LABEL_PREFIX + cat in labels)
    resolved = sum(is_done(fields) for fields in issues)
    escalated = sum(ESCALATED_LABEL in (fields.get("labels") or []) for fields in issues)

    opened = f"• *Opened:* {len(issues)}"
    if kinds:
        opened += " (" + ", ".join(_count(n, KIND_NAMES[kind].lower()) for kind, n in kinds.most_common()) + ")"
    lines = [
        title,
        opened,
        f"• *Resolved:* {resolved}   *Escalated:* {escalated}   *Still open:* {len(issues) - resolved}",
    ]
    if categories:
        top = ", ".join(f"{CATEGORY_NAMES[cat]} ({n})" for cat, n in categories.most_common(3))
        lines.append(f"• *Top categories:* {top}")
    lines.append(f"<{jira.base_url}/issues/?jql={quote(jql)}|Open these tickets in Jira>")
    return "\n".join(lines)


def _count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")
