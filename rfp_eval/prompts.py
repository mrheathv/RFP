"""Load and render the editable prompt templates in ``prompts/``.

Templates are plain Markdown with two structural markers, ``--- SYSTEM ---``
and ``--- USER ---``. Everything else -- including the evaluation criteria and
the scoring rubric -- is prose the user can tune without touching Python.

HTML comments at the top of a template document its placeholders and are
stripped before the prompt is sent.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .errors import RfpEvalError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_SYSTEM_MARKER = "--- SYSTEM ---"
_USER_MARKER = "--- USER ---"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class PromptError(RfpEvalError):
    """A prompt template is missing or malformed."""


class PromptTemplate:
    def __init__(self, name: str, system: str, user: str):
        self.name = name
        self.system = system
        self.user = user

    def render(self, **values) -> tuple[str, str]:
        """Return ``(system, user)`` with placeholders substituted.

        A missing placeholder is a template bug, so it raises rather than
        silently sending the model a prompt with a literal ``{brace}`` in it.
        """
        try:
            return self.system.format(**values), self.user.format(**values)
        except KeyError as exc:
            raise PromptError(
                f"prompt '{self.name}' uses placeholder {exc} but it was not supplied"
            ) from exc
        except (IndexError, ValueError) as exc:
            raise PromptError(
                f"prompt '{self.name}' has a malformed placeholder: {exc}. "
                "Literal braces in a template must be doubled: {{ and }}."
            ) from exc


@lru_cache(maxsize=None)
def load_prompt(name: str, prompts_dir: str | None = None) -> PromptTemplate:
    """Load ``prompts/<name>.md``. Cached -- call ``load_prompt.cache_clear()``
    after editing a template in a long-running process."""
    directory = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
    path = directory / f"{name}.md"

    if not path.exists():
        raise PromptError(f"prompt template not found: {path}")

    raw = _COMMENT_RE.sub("", path.read_text(encoding="utf-8"))

    if _SYSTEM_MARKER not in raw or _USER_MARKER not in raw:
        raise PromptError(
            f"prompt '{name}' must contain both '{_SYSTEM_MARKER}' and '{_USER_MARKER}' markers"
        )

    _, _, after_system = raw.partition(_SYSTEM_MARKER)
    system, _, user = after_system.partition(_USER_MARKER)

    system, user = system.strip(), user.strip()
    if not system or not user:
        raise PromptError(f"prompt '{name}' has an empty SYSTEM or USER section")

    return PromptTemplate(name, system, user)


def format_questions(questions) -> str:
    """Render the canonical question list for a prompt.

    The ``- id: ... | category: ...`` shape is deliberate: it is compact, it is
    unambiguous for the model, and the fake provider parses it back out.
    """
    lines = []
    for q in questions:
        required = "yes" if q.required else "no"
        lines.append(f"- id: {q.id} | category: {q.category} | required: {required}")
        lines.append(f"  text: {q.text}")
    return "\n".join(lines)
