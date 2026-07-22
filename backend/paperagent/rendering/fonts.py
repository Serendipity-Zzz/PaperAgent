from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont, TTLibError
from pydantic import Field

from paperagent.schemas.common import StrictModel

FONT_ALIASES = {
    "宋体": ("SimSun", "Noto Serif CJK SC"),
    "新宋体": ("NSimSun", "Noto Serif CJK SC"),
    "黑体": ("SimHei", "Noto Sans CJK SC"),
    "微软雅黑": ("Microsoft YaHei", "Noto Sans CJK SC"),
    "仿宋": ("FangSong", "Noto Serif CJK SC"),
    "楷体": ("KaiTi", "Noto Serif CJK SC"),
}


class FontResolution(StrictModel):
    requested: str
    resolved: str | None = None
    installed: bool = False
    fallback_used: bool = False
    fallback_candidates: list[str] = Field(default_factory=list)
    requires_user_action: bool = False
    message: str


class FontResolver:
    def __init__(self, font_directories: list[Path] | None = None) -> None:
        self.font_directories = font_directories or self._default_directories()
        self._installed = _scan_directories(
            tuple(str(item.resolve()) for item in self.font_directories)
        )

    def resolve(self, requested: str, *, allow_fallback: bool = False) -> FontResolution:
        normalized = _normalize(requested)
        installed = self._installed.get(normalized)
        aliases = FONT_ALIASES.get(requested.strip(), ())
        candidates = [requested, *aliases]
        if installed:
            return FontResolution(
                requested=requested,
                resolved=installed,
                installed=True,
                message="requested font is installed",
            )
        for candidate in candidates[1:]:
            resolved = self._installed.get(_normalize(candidate))
            if resolved and allow_fallback:
                return FontResolution(
                    requested=requested,
                    resolved=resolved,
                    installed=True,
                    fallback_used=True,
                    fallback_candidates=list(aliases),
                    message="approved alias fallback is installed",
                )
        return FontResolution(
            requested=requested,
            fallback_candidates=list(aliases),
            requires_user_action=True,
            message="font is missing; choose a fallback or approve font installation",
        )

    @staticmethod
    def _default_directories() -> list[Path]:
        directories: list[Path] = []
        windir = os.environ.get("WINDIR")
        if windir:
            directories.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            directories.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return directories


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


@lru_cache(maxsize=8)
def _scan_directories(directories: tuple[str, ...]) -> dict[str, str]:
    installed: dict[str, str] = {}
    for raw_directory in directories:
        directory = Path(raw_directory)
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.suffix.casefold() not in {".ttf", ".otf", ".ttc"}:
                continue
            names = _font_family_names(path)
            preferred = next(iter(names), path.stem)
            installed.setdefault(_normalize(path.stem), preferred)
            for name in names:
                installed.setdefault(_normalize(name), name)
    return installed


def _font_family_names(path: Path) -> list[str]:
    fonts: list[TTFont] = []
    collection: TTCollection | None = None
    try:
        if path.suffix.casefold() == ".ttc":
            collection = TTCollection(str(path), lazy=True)
            fonts = list(collection.fonts)
        else:
            fonts = [TTFont(str(path), lazy=True)]
        names: list[str] = []
        for font in fonts:
            table = font.get("name")
            if table is None:
                continue
            records = [item for item in table.names if item.nameID in {1, 16}]
            records.sort(key=lambda item: 0 if item.nameID == 16 else 1)
            for record in records:
                try:
                    value = record.toUnicode().strip()
                except (UnicodeDecodeError, AttributeError):
                    continue
                if value and value not in names:
                    names.append(value)
        return names
    except (OSError, TTLibError, ValueError):
        return []
    finally:
        if collection is not None:
            collection.close()
        else:
            for font in fonts:
                font.close()
