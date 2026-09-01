"""Copy the local JSON library into Supabase, or pull it back down.

    python scripts/sync_to_supabase.py push [--dry-run]
    python scripts/sync_to_supabase.py pull [--dry-run]

`push` uploads every local profile, overwriting the cloud copy of anything with
the same id. `pull` does the reverse, so the cloud library can be brought back
to a laptop for offline work or kept in git as a backup.

Neither direction deletes: a profile that exists only at the destination is left
alone and reported. Silent deletion across a sync is how people lose work, and
the tool has no way to tell "this was deleted deliberately" from "this had not
been pushed yet".

Credentials come from the environment or `.streamlit/secrets.toml`:

    export SUPABASE_URL=https://xxxx.supabase.co
    export SUPABASE_KEY=eyJ...
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dronecalc.backends import FileBackend, SupabaseBackend  # noqa: E402
from dronecalc.store import KINDS, Library  # noqa: E402

SECRETS = ROOT / ".streamlit" / "secrets.toml"


def credentials() -> tuple[str, str]:
    """Supabase URL and key, from the environment or the local secrets file."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not (url and key) and SECRETS.is_file():
        data = tomllib.loads(SECRETS.read_text())
        url = url or str(data.get("SUPABASE_URL", "")).strip()
        key = key or str(data.get("SUPABASE_KEY", "")).strip()
    if not (url and key):
        raise SystemExit(
            "SUPABASE_URL and SUPABASE_KEY are not set, and "
            f"{SECRETS.relative_to(ROOT)} does not supply them.\n"
            "Copy .streamlit/secrets.toml.example and fill it in."
        )
    return url, key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("direction", choices=["push", "pull"])
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    url, key = credentials()
    local = Library.load(backend=FileBackend())
    cloud = Library.load(backend=SupabaseBackend(url, key))

    for lib, name in ((local, "local"), (cloud, "cloud")):
        for err in lib.load_errors:
            print(f"  ! {name} profile skipped — {err}")

    source, destination = (local, cloud) if args.direction == "push" else (cloud, local)
    print(f"{args.direction}: {source.backend.label}  ->  {destination.backend.label}")
    if args.dry_run:
        print("(dry run — nothing will be written)")

    written = 0
    for kind in KINDS:
        for obj_id in source.list_ids(kind):
            obj = getattr(source, kind)[obj_id]
            existing = getattr(destination, kind).get(obj_id)
            if existing is not None and existing.model_dump() == obj.model_dump():
                continue
            verb = "update" if existing is not None else "create"
            print(f"  {verb:>6}  {kind}/{obj_id}")
            if not args.dry_run:
                destination.save(kind, obj)
            written += 1

    for kind in KINDS:
        for obj_id in destination.list_ids(kind):
            if obj_id not in getattr(source, kind):
                print(f"  {'keep':>6}  {kind}/{obj_id}  (only at the destination — not deleted)")

    print(f"\n{written} profile(s) {'would be ' if args.dry_run else ''}written.")


if __name__ == "__main__":
    main()
