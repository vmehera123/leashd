"""Directory boundary enforcement."""

from pathlib import Path

import structlog

logger = structlog.get_logger()


class SandboxEnforcer:
    def __init__(self, allowed_directories: list[Path]) -> None:
        self._allowed: list[Path] = [
            d.expanduser().resolve() for d in allowed_directories
        ]

    def validate_path(self, path: str | Path) -> tuple[bool, str]:
        try:
            resolved = Path(path).expanduser().resolve()
        except (ValueError, OSError) as e:
            logger.debug("sandbox_path_invalid", path=str(path), error=str(e))
            return False, f"Invalid path: {e}"

        for allowed in self._allowed:
            try:
                resolved.relative_to(allowed)
                return True, ""
            except ValueError:
                continue

        allowed_str = ", ".join(str(d) for d in self._allowed)
        logger.debug("sandbox_path_denied", path=str(resolved))
        return False, (f"Path {resolved} is outside allowed directories: {allowed_str}")

    def update_directories(self, directories: list[Path]) -> None:
        self._allowed = [d.expanduser().resolve() for d in directories]
        logger.info("sandbox_directories_updated", count=len(self._allowed))

    def add_directory(self, directory: Path) -> None:
        resolved = directory.expanduser().resolve()
        if resolved not in self._allowed:
            self._allowed.append(resolved)
            logger.debug("sandbox_directory_added", directory=str(resolved))
