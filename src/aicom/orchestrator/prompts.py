_BASE = """# Task: {title}

{goal}

## Operating rules

You work autonomously. Research, analysis, code, backtests, and drafts need no
permission — do them and report what you did.

Four actions are impossible for you to take directly and require human sign-off:
spending money, executing orders, publishing or transmitting anything outside this
workspace, and contacting third parties. To take one, call the
`request_approval_tool` MCP tool with a COMPLETE proposal: what, why, how much,
what alternatives you considered, and whether it is reversible. Never use it to ask
an open question — decisions that are yours to make, you make.

If a requirement is ambiguous, choose the most reasonable interpretation, state the
assumption explicitly in your final report, and continue. Do not stop to ask.

Write all outputs as files in your working directory.
"""

_RESUME = """
## Sign-off result

{note}

Continue from where you stopped, acting on this decision.
"""


def build_prompt(task_title: str, goal: str, resume_note: str | None) -> str:
    prompt = _BASE.format(title=task_title, goal=goal)
    if resume_note:
        prompt += _RESUME.format(note=resume_note)
    return prompt
