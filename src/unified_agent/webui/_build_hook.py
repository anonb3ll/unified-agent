"""Hatchling-only adapter; excluded from the installed wheel."""

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Hatch loads this file before the source package is installed. Import only
# the adjacent stdlib-only assembly helper, never the hub/config/database.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from build_resources import release_build_requested, resource_mappings
finally:
    sys.path.pop(0)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        build_data["force_include"].update(
            resource_mappings(
                Path(self.root),
                self.target_name,
                version,
                release=release_build_requested(),
                warn=self.app.display_warning,
            )
        )
