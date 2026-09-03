"""Import-time patch registry.

The fork changes backend behavior by replacing module-level symbols after
upstream's modules import, never by editing them. That keeps every upstream file
byte-identical, which is the difference between a weekly sync that costs an hour
and one that costs a day.

The risk of patching instead of editing is silence: upstream renames a function,
the patch stops finding it, and the fork quietly runs upstream behavior in a
deployment that cannot support it -- a self-hosted install would try to reach
Google Cloud Tasks and fail at request time instead of at boot. So every patch
declares the exact symbol it replaces and is verified before anything is
swapped. A missing target raises at startup, in CI, on the first sync that
renames it.

Patches are also profile-gated: `omi_cloud` applies none, so an upstream-mode
test run and an upstream-mode build behave exactly as upstream does.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class PatchError(RuntimeError):
    """A patch could not be applied. Always fatal: never degrade silently."""


@dataclass(frozen=True)
class Patch:
    """One module-level symbol replacement.

    module/attribute name the upstream symbol; `build` returns the replacement
    and receives the original so a patch can wrap rather than discard it.
    `applies_to` decides from the resolved profile whether this patch is active.
    """

    name: str
    module: str
    attribute: str
    build: Callable[[Any], Any]
    applies_to: Callable[[dict], bool]
    reason: str

    def target(self) -> tuple[Any, Any]:
        try:
            module = importlib.import_module(self.module)
        except ImportError as error:
            raise PatchError(
                f"patch '{self.name}': cannot import {self.module} ({error}). "
                f"If upstream moved this module, update the patch -- do not edit upstream."
            ) from error
        if not hasattr(module, self.attribute):
            raise PatchError(
                f"patch '{self.name}': {self.module} has no '{self.attribute}'. "
                f"Upstream most likely renamed or removed it; fix the patch, "
                f"do not reintroduce a fork edit in {self.module}."
            )
        return module, getattr(module, self.attribute)


@dataclass
class Registry:
    patches: list[Patch] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, patch: Patch) -> None:
        if any(p.name == patch.name for p in self.patches):
            raise PatchError(f"duplicate patch name '{patch.name}'")
        self.patches.append(patch)

    def verify(self, profile: dict) -> None:
        """Resolve every applicable target without swapping anything.

        Separate from `apply` so a check can prove the registry still matches
        upstream without mutating the process.
        """
        for patch in self.patches:
            if patch.applies_to(profile):
                patch.target()

    def apply(self, profile: dict) -> "Registry":
        # Verify first: a half-patched process is worse than an unpatched one.
        self.verify(profile)
        for patch in self.patches:
            if not patch.applies_to(profile):
                self.skipped.append(patch.name)
                continue
            module, original = patch.target()
            setattr(module, patch.attribute, patch.build(original))
            self.applied.append(patch.name)
        return self

    def summary(self) -> str:
        applied = ", ".join(self.applied) or "none"
        return f"fork patches applied: {applied} ({len(self.skipped)} not applicable)"


def build_registry(patches: Iterable[Patch]) -> Registry:
    registry = Registry()
    for patch in patches:
        registry.add(patch)
    return registry
