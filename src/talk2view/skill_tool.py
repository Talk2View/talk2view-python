"""``save_skill``: a client tool the chat server adds when the end-user has a skills
store (``ui.server.UIServer``, when ``skills_dir`` is given and ``features.skills`` is
on). Every call asks the end-user (``permission=True``, no "always"): the page draws it
as a skill card with Save / Edit / Cancel. A saved skill is standing
instructions for the assistant, so nothing is written without that explicit Save, and a
headless controller with no ``UIServer`` never gets this tool at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .skill_format import from_form, validate
from .skill_store import InvalidSkillError, SkillExistsError
from .skills import SkillValidationError
from .types import ClientToolSchema

if TYPE_CHECKING:
    from .skills import T2VSkills

SAVE_SKILL = "save_skill"

SAVE_SKILL_SCHEMA = ClientToolSchema(
    name=SAVE_SKILL,
    description=(
        "Save a reusable skill for the user, who reviews it first. Call only when they ask "
        "to save or remember something, or accept your offer; offer once at most, for a "
        "lasting preference."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "lowercase-hyphenated, e.g. report-volumes"},
            "description": {
                "type": "string",
                "description": "Third person: what it does, then 'Use when ...'",
            },
            "instructions": {
                "type": "string",
                "description": "Concise markdown steps in consistent terms; only what you "
                "wouldn't know; no dates",
            },
            "replaces": {"type": "string", "description": "The user's skill this updates"},
        },
        "required": ["name", "description", "instructions"],
    },
)


def save_skill_handler(skills: "T2VSkills") -> Callable[..., str]:
    """A handler for ``SAVE_SKILL``. Returns a callable taking the call's arguments as
    keyword arguments -- the production convention ``T2VTools.execute_tool_call`` uses
    for every handler (``handler(**clean_args)``): ``name``, ``description``,
    ``instructions``, and an optional ``replaces``. An unknown keyword (a stray extra key
    a model sent) is silently ignored, never a ``TypeError``.

    Never raises: bad input, a taken name, a write failure, or no writable store at all
    are all reported back to the model as a result string, so it can retry or give up.
    """

    def run(**kwargs: Any) -> str:
        doc = from_form(
            str(kwargs.get("name", "")),
            str(kwargs.get("description", "")),
            str(kwargs.get("instructions", "")),
        )
        problems = validate(doc)
        if problems:
            return "Not saved. Fix and call save_skill again: " + "; ".join(
                "{}: {}".format(p.field, p.message) for p in problems
            )
        replaces = kwargs.get("replaces") or None
        try:
            name = skills.save_doc(doc, replaces=replaces)
        except KeyError:
            # `replaces` names none of the user's own skills (a bundled or app skill, a
            # typo, or anything path-like): nothing is written.
            return (
                "Not saved. The user has no skill named {!r} to replace; leave replaces out "
                "to save a new skill."
            ).format(replaces)
        except SkillExistsError as exc:
            return (
                "Not saved. The user already has a skill named {!r}; pass replaces={!r} "
                "to update it, or pick another name."
            ).format(exc.name, exc.name)
        except InvalidSkillError as exc:
            return "Not saved. Fix and call save_skill again: {}".format(exc)
        except SkillValidationError:
            # No writable store at all (headless, or the host gave none): save_doc()
            # never even reaches the store's own write.
            return "Not saved: this app keeps no skills of its own."
        except OSError as exc:
            return "Not saved: the skill could not be written ({}).".format(exc.strerror or exc)
        return "Saved skill {!r}. It applies from the user's next message.".format(name)

    return run


__all__ = ["SAVE_SKILL", "SAVE_SKILL_SCHEMA", "save_skill_handler"]
