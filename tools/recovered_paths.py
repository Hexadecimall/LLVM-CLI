"""Relocate paths recorded by an earlier build into this project's recovered tree."""

from pathlib import Path
import re


LEGACY_CACHE_PATH = re.compile(
    r"/(?:Users|home)/[^/\s]+/\.cache/(llvm-all|piper-llvm)(?=/|$)"
)
LEGACY_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+(?=/|$)")


def relocate(value: str, project: Path) -> str:
    return LEGACY_CACHE_PATH.sub(
        lambda match: str(project / "recovered" / match.group(1)), value
    )


def recorded_home_paths(value: str) -> set[str]:
    return set(LEGACY_HOME_PATH.findall(value))
