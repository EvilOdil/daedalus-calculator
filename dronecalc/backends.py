"""Where profiles are stored.

`Library` holds the domain logic - resolution, compatibility, id lookup - and
knows nothing about where the bytes live. A backend supplies three operations:
read every document of a kind, write one, delete one. Documents are plain JSON
dicts; validation into Pydantic models stays in `Library`, so a malformed row in
the database fails exactly the way a malformed file does.

Two backends ship:

* `FileBackend`   - one JSON file per profile under `data/<kind>/<id>.json`.
                    The local development and offline default.
* `SupabaseBackend` - one Postgres row per profile, `(kind, id) -> document`.
                    Used when the app runs in the cloud.

The split exists because the two have genuinely different failure modes. A file
store cannot lose a write to a network partition; a database cannot be corrupted
by a text editor. Keeping them behind one interface means the solver, the tests
and the UI never have to care which one is underneath.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Table that holds every profile in Postgres. One row per profile.
SUPABASE_TABLE = "profiles"


class BackendError(RuntimeError):
    """A backend could not complete an operation."""


@runtime_checkable
class StorageBackend(Protocol):
    """Read and write profile documents, keyed by kind and id."""

    #: Shown in the UI so it is never ambiguous which store is in use.
    label: str

    def read_all(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        """Every document of `kind`, as `(source_name, document)` pairs.

        `source_name` names the document for error messages - a filename, a row
        id - so a load failure can be reported against something the user can
        actually go and look at.
        """
        ...

    def write(self, kind: str, obj_id: str, document: dict[str, Any]) -> str:
        """Store one document. Returns a human-readable location."""
        ...

    def delete(self, kind: str, obj_id: str) -> None:
        """Remove one document. Missing documents are not an error."""
        ...


class FileBackend:
    """One JSON file per profile, under `data/<kind>/<id>.json`."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.label = f"local files ({self.data_dir})"

    def read_all(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        directory = self.data_dir / kind
        if not directory.is_dir():
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                out.append((f"{kind}/{path.name}", json.loads(path.read_text())))
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                out.append((f"{kind}/{path.name}", {"__error__": str(exc)}))
        return out

    def write(self, kind: str, obj_id: str, document: dict[str, Any]) -> str:
        directory = self.data_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{obj_id}.json"
        path.write_text(json.dumps(document, indent=2) + "\n")
        return str(path)

    def delete(self, kind: str, obj_id: str) -> None:
        path = self.data_dir / kind / f"{obj_id}.json"
        if path.exists():
            path.unlink()


class SupabaseBackend:
    """Profiles as rows in a Postgres table: `(kind, id) -> document jsonb`.

    The whole profile is kept as one JSONB document rather than shredded into
    columns. The Pydantic models already are the schema, and they change often
    while the app is young; a migration per new datasheet field would be a tax
    with no benefit, since nothing queries inside a profile - the app always
    loads the library whole and works in memory.
    """

    def __init__(self, url: str, key: str) -> None:
        try:
            from supabase import create_client
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise BackendError(
                "the supabase package is not installed. `pip install supabase`, "
                "or unset SUPABASE_URL to fall back to local files."
            ) from exc
        self._client = create_client(url, key)
        host = url.split("//")[-1].split(".")[0]
        self.label = f"Supabase ({host})"

    def read_all(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        try:
            rows = (
                self._client.table(SUPABASE_TABLE)
                .select("id, document")
                .eq("kind", kind)
                .order("id")
                .execute()
                .data
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not read {kind} from Supabase: {exc}") from exc
        return [(f"{kind}/{row['id']}", row["document"]) for row in rows]

    def write(self, kind: str, obj_id: str, document: dict[str, Any]) -> str:
        try:
            self._client.table(SUPABASE_TABLE).upsert(
                {"kind": kind, "id": obj_id, "document": document},
                on_conflict="kind,id",
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not save {kind}/{obj_id}: {exc}") from exc
        return f"{SUPABASE_TABLE}:{kind}/{obj_id}"

    def delete(self, kind: str, obj_id: str) -> None:
        try:
            self._client.table(SUPABASE_TABLE).delete().eq("kind", kind).eq(
                "id", obj_id
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not delete {kind}/{obj_id}: {exc}") from exc


def default_backend() -> StorageBackend:
    """Pick a backend from the environment.

    Supabase when `SUPABASE_URL` and `SUPABASE_KEY` are both set, local files
    otherwise. Deliberately not a silent fallback: if the URL is present but the
    connection fails, that is raised rather than quietly writing to a local file
    the cloud will never see.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if url and key:
        return SupabaseBackend(url, key)
    return FileBackend()
