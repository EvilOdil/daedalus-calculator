"""Component and setup library.

A setup references components by id, so mixing and matching never duplicates a
profile and every setup that uses a component picks up a datasheet correction
automatically.

Where the profiles physically live is `backends`' problem, not this module's:
local JSON files during development, Postgres in the cloud. Everything here
works on validated Pydantic objects held in memory.
"""

from __future__ import annotations


from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from .backends import DEFAULT_DATA_DIR, FileBackend, StorageBackend, default_backend
from .missions import Mission
from .models import Battery, ESC, Frame, Motor, Payload, Propeller, ResolvedSetup, Setup

#: Directory name -> model class.
KINDS: dict[str, type[BaseModel]] = {
    "motors": Motor,
    "props": Propeller,
    "escs": ESC,
    "batteries": Battery,
    "frames": Frame,
    "payloads": Payload,
    "setups": Setup,
    "missions": Mission,
}


class LibraryError(RuntimeError):
    pass


@dataclass
class Library:
    """All profiles on disk, loaded and indexed by id."""

    #: Where profiles are read from and written to. Defaults to whatever the
    #: environment implies - see `backends.default_backend`.
    backend: StorageBackend = field(default_factory=default_backend)
    #: Profiles that would not load, as "source: reason". Surfaced in the UI so a
    #: single bad profile does not take the whole library down with it.
    load_errors: list[str] = field(default_factory=list)
    motors: dict[str, Motor] = field(default_factory=dict)
    props: dict[str, Propeller] = field(default_factory=dict)
    escs: dict[str, ESC] = field(default_factory=dict)
    batteries: dict[str, Battery] = field(default_factory=dict)
    frames: dict[str, Frame] = field(default_factory=dict)
    payloads: dict[str, Payload] = field(default_factory=dict)
    setups: dict[str, Setup] = field(default_factory=dict)
    missions: dict[str, Mission] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        """The directory behind a file-backed library.

        Kept because scripts and tests address the local store by path. Asking a
        cloud-backed library for a directory is a category error, so it says so
        rather than inventing one.
        """
        if isinstance(self.backend, FileBackend):
            return self.backend.data_dir
        raise LibraryError(f"this library is backed by {self.backend.label}, not a directory")

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        *,
        backend: StorageBackend | None = None,
    ) -> "Library":
        """Load every profile. `data_dir` forces the file backend at that path."""
        if backend is None:
            backend = FileBackend(data_dir) if data_dir is not None else default_backend()
        lib = cls(backend=backend)
        lib.reload()
        return lib

    def reload(self) -> None:
        """Load every profile, skipping and recording any that will not validate.

        Profiles are hand-editable JSON, so one malformed file must not make the
        rest of the library unreachable. Failures are collected in `load_errors`
        for the caller to display.
        """
        errors: list[str] = []
        for kind, model in KINDS.items():
            bucket: dict[str, Any] = {}
            try:
                documents = self.backend.read_all(kind)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                errors.append(f"{kind}: {str(exc).splitlines()[0]}")
                setattr(self, kind, bucket)
                continue
            for source, document in documents:
                try:
                    obj = model.model_validate(document)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    errors.append(f"{source}: {str(exc)}".replace("\n", " "))
                    continue
                if obj.id in bucket:
                    errors.append(f"{source}: duplicate id '{obj.id}'")
                bucket[obj.id] = obj
            setattr(self, kind, bucket)
        self.load_errors = errors

    # ------------------------------------------------------------------ #

    def _bucket(self, kind: str) -> dict[str, Any]:
        if kind not in KINDS:
            raise LibraryError(f"unknown kind '{kind}'")
        return getattr(self, kind)

    def save(self, kind: str, obj: BaseModel) -> str:
        """Store a profile and index it. Returns where it went."""
        self._bucket(kind)  # rejects an unknown kind before anything is written
        location = self.backend.write(kind, obj.id, obj.model_dump(mode="json"))
        self._bucket(kind)[obj.id] = obj
        return location

    def delete(self, kind: str, obj_id: str) -> None:
        self.backend.delete(kind, obj_id)
        self._bucket(kind).pop(obj_id, None)

    def _get(self, kind: str, obj_id: str) -> Any:
        bucket = self._bucket(kind)
        if obj_id not in bucket:
            known = ", ".join(sorted(bucket)) or "(none)"
            raise LibraryError(f"no {kind[:-1]} with id '{obj_id}'. Known: {known}")
        return bucket[obj_id]

    def resolve(self, setup: Setup | str) -> ResolvedSetup:
        """Load a setup's component profiles into an independent working copy.

        Everything is deep-copied. A resolved setup is a scratch object - sweeps
        and sensitivity runs routinely tweak a battery's mass or zero a payload's
        power - and handing out references to the library's own profiles would let
        one of those edits silently rewrite every other setup that shares the
        component.
        """
        if isinstance(setup, str):
            setup = self._get("setups", setup)
        return ResolvedSetup(
            setup=setup.model_copy(deep=True),
            frame=self._get("frames", setup.frame_id).model_copy(deep=True),
            motor=self._get("motors", setup.motor_id).model_copy(deep=True),
            prop=self._get("props", setup.prop_id).model_copy(deep=True),
            esc=self._get("escs", setup.esc_id).model_copy(deep=True),
            battery=self._get("batteries", setup.battery_id).model_copy(deep=True),
            payload=(
                self._get("payloads", setup.payload_id).model_copy(deep=True)
                if setup.payload_id
                else None
            ),
        )

    def compatibility_errors(self, r: ResolvedSetup) -> list[str]:
        """Hard incompatibilities: the combination cannot be built or flown.

        These are checked before any performance number is believed. A propeller
        that fouls the airframe or a pack that overvolts the motor is not a
        marginal design, it is not a design.
        """
        out: list[str] = []
        cells = r.battery.cells_s
        if r.motor.max_cells_s and cells > r.motor.max_cells_s:
            out.append(
                f"battery is {cells}S but the {r.motor.name} is rated to {r.motor.max_cells_s}S"
            )
        if r.esc.max_cells_s and cells > r.esc.max_cells_s:
            out.append(f"battery is {cells}S but the {r.esc.name} is rated to {r.esc.max_cells_s}S")
        if r.frame.max_prop_in and r.prop.diameter_in > r.frame.max_prop_in:
            out.append(
                f'{r.prop.diameter_in:g}" propeller will not fit the {r.frame.name}, which takes '
                f'{r.frame.max_prop_in:g}" at most'
            )
        return out

    def compatibility_warnings(self, r: ResolvedSetup) -> list[str]:
        """Soft problems: the numbers may be optimistic or unverifiable."""
        out: list[str] = []
        if r.motor.table_for(r.prop.id) is None:
            out.append(
                f"no measured thrust table for {r.motor.name} + {r.prop.name}: "
                "falling back to momentum theory (results are ESTIMATED)"
            )
        wanted = r.setup.thrust_table
        if wanted:
            available = [t.display_name for t in r.motor.tables_for(r.prop.id)]
            if wanted not in available:
                out.append(
                    f'this setup asks for the thrust table "{wanted}", which {r.motor.name} '
                    f"no longer has. Using {available[0]!r} instead."
                    if available
                    else f'this setup asks for the thrust table "{wanted}", which does not exist.'
                )
        if not r.motor.max_current_a and not r.motor.max_power_w:
            out.append(
                f"{r.motor.name} declares no current or power rating, so nothing stops the "
                "model from over-working it. A small motor swinging a large propeller will "
                "look efficient here and overheat in reality — add the ratings from its "
                "datasheet before trusting any result."
            )
        if r.frame.max_prop_in and r.prop.diameter_in > r.frame.max_prop_in * 0.95:
            out.append(
                f'{r.prop.diameter_in:g}" propeller is at the limit of what the '
                f"{r.frame.name} accepts — check tip clearance."
            )
        return out

    def list_ids(self, kind: str) -> list[str]:
        return sorted(self._bucket(kind))

    def all_setups(self) -> Iterable[Setup]:
        return [self.setups[k] for k in sorted(self.setups)]

    def all_missions(self) -> Iterable[Mission]:
        return [self.missions[k] for k in sorted(self.missions)]

    def missions_for_setup(self, setup_id: str) -> list[Mission]:
        return [m for m in self.all_missions() if m.setup_id == setup_id]
