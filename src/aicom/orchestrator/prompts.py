_BASE = """# Task: {title}

{goal}

## Operating rules

{persona_block}You work autonomously. Research, analysis, code, backtests, and drafts need no
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


def build_prompt(task_title: str, goal: str, persona: str, resume_note: str | None) -> str:
    # persona sits at the top of "## Operating rules", immediately alongside the
    # autonomy-boundary text every agent gets -- not appended after the goal, and not
    # a separate trailing section, so an agent cannot read the goal and start acting
    # before it has seen what kind of agent it is supposed to be. An agent with no
    # persona configured (persona == "", true of every agent row before this pack)
    # gets exactly the prompt it always got: persona_block is empty, so the standard
    # operating rules paragraph is the first thing under the heading, unchanged.
    #
    # This ordering is NOT a security boundary and must not be treated as one: there
    # is no property of how these models process text that makes earlier prose bind
    # more strongly than later prose -- if anything, recency tends to matter more, not
    # less. A persona that said "ignore the instructions that follow" would not be
    # neutralised by appearing first. The actual guarantee that an operator-supplied
    # persona cannot escape the autonomy boundary lives entirely outside this prompt
    # text: `--allowedTools` is computed in code from `agent.allowed_tools` plus the
    # gate tool (see worker.py's `_build_request`), the gate MCP server is injected by
    # code rather than by convention, and an approved gated tool is whitelisted for
    # exactly one execution. Persona prose cannot change any of that -- prompt
    # ordering here is a readability choice, not a control.
    persona_block = f"{persona.strip()}\n\n" if persona.strip() else ""
    prompt = _BASE.format(title=task_title, goal=goal, persona_block=persona_block)
    if resume_note:
        prompt += _RESUME.format(note=resume_note)
    return prompt
