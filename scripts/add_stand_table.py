"""Add the 2216 thrust-stand run to the live library, in place.

Kept as a script rather than folded into a reseed so nothing else in `data/` is
touched. Idempotent: re-running replaces the measured table rather than stacking
duplicates.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dronecalc.models import Provenance, ThrustRow, ThrustTable
from dronecalc.store import Library

# Inlined deliberately. Importing it from `seed_library` would execute that
# module, which reseeds the whole library and discards any edits made in the UI.
STAND_2216_TABLE = [
    # throttle %, thrust g, torque N*m, current A, rpm, bus power W, bus V
    (20, 52.8, 0.0088, 0.2344, 2381, 3.83, 16.34),
    (30, 134.2, 0.0223, 0.7569, 3692, 12.35, 16.32),
    (50, 366.7, 0.0575, 3.042, 5980, 49.37, 16.23),
    (60, 509.4, 0.0785, 4.849, 7049, 78.29, 16.14),
    (70, 677.4, 0.1023, 7.29, 8136, 116.9, 16.03),
    (80, 872.4, 0.1296, 10.34, 9215, 164.4, 15.90),
    (90, 1068.0, 0.1552, 13.91, 10255, 218.8, 15.73),
    (100, 1120.0, 0.1607, 15.22, 10637, 237.8, 15.62),
]

lib = Library.load()
motor = lib.motors["holybro-2216-920kv"]

table = ThrustTable(
    prop_id="t1045ii", test_voltage_v=15.62, esc_id="blheli-s-20a",
    rows=[ThrustRow(throttle_pct=t, thrust_g=th, torque_nm=q, current_a=i, rpm=n, power_w=p)
          for t, th, q, i, n, p, _v in STAND_2216_TABLE],
    provenance=Provenance(
        retrieved="2026-09-01", confidence="measured",
        notes="Thrust-stand run on the actual motor, 8 usable points from 1200 to 2000 us. "
              "Bus voltage sags 16.35 -> 15.62 V across the sweep; test_voltage_v records "
              "15.62 V, the voltage at the top row, because that is the condition the "
              "max-thrust ceiling is scaled from. Per-row power is used directly by the "
              "fitter, so the sag does not distort it. "
              "UNRESOLVED: thrust-vs-rpm sits 25% below the T-Motor datasheet, but scaling "
              "rpm by 12/14 brings it onto the datasheet curve within 1.5% while leaving "
              "power 17% worse - the signature of a 12-pole stand setting on a 14-pole "
              "(12N14P) motor. If the pole count was wrong, every rpm here is 16.7% high "
              "and Ct/Cq are 27% low. Confirm before trusting Ct."),
)

kept = [t for t in motor.thrust_tables if t.provenance.confidence != "measured"]
motor.thrust_tables = [table] + kept
print("saved:", lib.save("motors", motor))
for t in motor.thrust_tables:
    print(f"  {t.provenance.confidence:>9}  prop={t.prop_id}  V={t.test_voltage_v}  rows={len(t.rows)}")
