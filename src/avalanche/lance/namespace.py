"""Lance namespace/catalog backend for Avalanche."""

from __future__ import annotations

import shutil
from pathlib import Path

from avalanche.storage import Namespace, NamespaceConfig, TableGroup

from .table import LanceTable


class LanceTableGroup(TableGroup):
    """Group of Lance tables within a namespace."""

    def __init__(self, **tables: LanceTable):
        super().__init__(**tables)


class LanceNamespaceConfig(NamespaceConfig):
    """Configuration for a Lance namespace."""


class LanceNamespace(Namespace):
    """Filesystem-backed namespace for Lance datasets."""

    ns_config: LanceNamespaceConfig | None = None

    def _get_all_tables(self) -> list[tuple[str, LanceTable]]:
        return super()._get_all_tables()

    def push(self) -> None:
        Path(self.location).mkdir(parents=True, exist_ok=True)
        for _, table in self._get_all_tables():
            Path(table.location).mkdir(parents=True, exist_ok=True)

    def drop(self, *, drop_tables: bool = False) -> None:
        namespace_path = Path(self.location)

        if drop_tables:
            for _, table in self._get_all_tables():
                shutil.rmtree(table.location, ignore_errors=True)

        try:
            namespace_path.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            # Leave non-empty namespaces in place when table deletion was not requested.
            return

    def __repr__(self) -> str:
        return f"LanceNamespace(name={self.name!r})"


LanceNs = LanceNamespace
LanceNsConfig = LanceNamespaceConfig
