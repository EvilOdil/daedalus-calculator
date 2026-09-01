"""Seed the component library from datasheets. Re-runnable; overwrites data/.

Everything here traces to a published document. Where a number is not published
it is left null or explicitly marked `estimated`, never invented quietly.
"""
import sys
sys.path.insert(0, ".")
from dronecalc.models import *
from dronecalc.store import Library

RET = "2026-08-31"
DS = ("https://cdn.robotshop.com/media/T/Tmo/RB-Tmo-266/pdf/"
      "ligpower_airgear_450ii_combo_set_multi_rotor_uav_p_datasheet.pdf")
HB = "https://holybro.com/products/x500-v2-kits"

lib = Library.load()

# --------------------------------------------------------------------- frame
lib.save("frames", Frame(
    id="x500-v2", name="Holybro X500 V2", manufacturer="Holybro",
    wheelbase_mm=500, weight_g=610, arms=4, max_prop_in=11,
    provenance=Provenance(
        source_url=HB, retrieved=RET, confidence="vendor",
        notes="610 g is the bare airframe: 2 mm carbon plates, 16 mm tube arms and landing "
              "gear. Motors, ESCs, propellers, PDB and avionics are counted separately.")))

# ---------------------------------------------------------------- propeller
lib.save("props", Propeller(
    id="t1045ii", name="T1045II 10x4.5", manufacturer="T-Motor",
    diameter_in=10.0, pitch_in=4.5, blades=2, weight_g=12.5,
    material="Nylon + glass fibre", propeller_type="Polymer propeller",
    surface_treatment="Matte", dimensions_mm="260 x 30",
    thrust_limit_g=1200, optimum_rpm_min=6000, optimum_rpm_max=7000,
    working_temp_c_min=-10, working_temp_c_max=40,
    provenance=Provenance(
        source_url=DS, retrieved=RET, confidence="datasheet",
        notes="T1045II-Specifications block. Equivalent to the 1045 propellers shipped in "
              "the Holybro X500 V2 kit.")))

# ---------------------------------------------------------------------- ESC
lib.save("escs", ESC(
    id="blheli-s-20a", name="BLHeli_S 20A", manufacturer="Holybro / T-Motor",
    cont_current_a=20, burst_current_a=30, burst_duration_s=10, weight_g=21,
    max_cells_s=4, efficiency=0.96, firmware="BLHeli_S",
    dimensions_mm="26 x 14 x 5", connector="2.0 mm bullet",
    signal_hz_min=50, signal_hz_max=600, bec_current_a=None,
    provenance=Provenance(
        source_url=DS, retrieved=RET, confidence="datasheet",
        notes="BLHeli S 20A-Specifications block. No BEC. The 96% conversion efficiency is a "
              "class-typical BLHeli_S figure, not a published one.")))

# -------------------------------------------------------------------- motor
AIR2216_TABLE = [
    # throttle %, thrust g, torque N*m, current A, rpm, power W, eff g/W
    (30, 210, 0.03, 1.44, 4042, 23, 9.12), (35, 259, 0.04, 1.87, 4469, 30, 8.67),
    (40, 309, 0.05, 2.29, 4855, 37, 8.45), (45, 373, 0.05, 2.86, 5301, 46, 8.15),
    (50, 447, 0.06, 3.60, 5780, 58, 7.76), (55, 536, 0.08, 4.53, 6298, 72, 7.39),
    (60, 628, 0.09, 5.61, 6800, 90, 7.01), (65, 729, 0.10, 6.78, 7281, 108, 6.73),
    (70, 814, 0.11, 7.92, 7679, 126, 6.44), (75, 906, 0.12, 9.20, 8096, 147, 6.18),
    (80, 993, 0.14, 10.59, 8468, 169, 5.88), (85, 1087, 0.15, 12.11, 8867, 193, 5.65),
    (90, 1191, 0.16, 13.81, 9257, 219, 5.43), (95, 1289, 0.18, 15.68, 9675, 249, 5.18),
    (100, 1332, 0.18, 16.37, 9857, 260, 5.13),
]

# Your own Flight Stand run on the same motor. Thrust is kgf in the raw export and
# converted to grams here; throttle is PWM microseconds, mapped 1000-2000 us -> 0-100%.
# The two junk rows at 1000 and 1100 us (zero/negative thrust, negative current) are
# dropped - they are stand noise around zero, not operating points.
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

lib.save("motors", Motor(
    id="holybro-2216-920kv", name="Holybro 2216 920KV", manufacturer="Holybro",
    kv_rpm_per_v=920, weight_g=64, stator_diameter_mm=22, stator_height_mm=16,
    io_a=0.8, io_test_v=10.0, rm_ohm=0.115, rm_convention="phase_to_phase",
    max_current_a=17, max_current_duration_s=180, max_power_w=272, max_cells_s=4,
    shaft_mm=3, length_mm=42.5, diameter_mm=27.7, mount_pattern="4-M3, 16/19 mm",
    wire_length_mm=150,
    provenance=Provenance(
        source_url=HB, retrieved=RET, confidence="datasheet",
        notes="CLASS-EQUIVALENT SUBSTITUTION. Holybro publish only KV, size and weight for "
              "their 2216 920KV. Electrical parameters, dimensions and the thrust table come "
              "from the T-Motor AIR2216II KV920 datasheet: same 22x16 stator, same KV, same "
              "4S rating, same 1045 propeller, and the part T-Motor sells into this kit "
              "class. Numbers are representative of the class, not measured on your motors. "
              "Rated voltage 4S (4.2 V/cell) / 16 V; internal resistance 115 +/- 10 mOhm; "
              "weight 64 +/- 2 g."),
    thrust_tables=[ThrustTable(
        # Measured run first and marked `measured`, so `Motor.table_for` picks it
        # over the datasheet. The datasheet table is kept, not deleted: it is the
        # cross-check that exposed the rpm discrepancy documented below.
        prop_id="t1045ii", test_voltage_v=15.62, esc_id="blheli-s-20a",
        rows=[ThrustRow(throttle_pct=t, thrust_g=th, torque_nm=q, current_a=i,
                        rpm=n, power_w=p)
              for t, th, q, i, n, p, _v in STAND_2216_TABLE],
        provenance=Provenance(
            retrieved="2026-09-01", confidence="measured",
            notes="Thrust-stand run on the actual motor, 8 usable points from 1200 to 2000 "
                  "us. Bus voltage sags 16.35 -> 15.62 V across the sweep; test_voltage_v "
                  "records 15.62 V, the voltage at the top row, because that is the "
                  "condition the max-thrust ceiling is scaled from. Per-row power is used "
                  "directly by the fitter, so the sag does not distort it. "
                  "UNRESOLVED: thrust-vs-rpm sits 25% below the T-Motor datasheet, but "
                  "scaling rpm by 12/14 brings it onto the datasheet curve within 1.5% "
                  "while leaving power 17% worse - the signature of a 12-pole stand setting "
                  "on a 14-pole (12N14P) motor. If the pole count was wrong, every rpm here "
                  "is 16.7% high and Ct/Cq are 27% low. Confirm before trusting Ct."))
        ,
        ThrustTable(
        prop_id="t1045ii", test_voltage_v=16.0, esc_id="blheli-s-20a",
        rows=[ThrustRow(throttle_pct=t, thrust_g=th, torque_nm=q, current_a=i,
                        rpm=n, power_w=p, efficiency_g_per_w=e)
              for t, th, q, i, n, p, e in AIR2216_TABLE],
        provenance=Provenance(
            source_url=DS, retrieved=RET, confidence="datasheet",
            notes="AIR2216II-KV920 + T1045II at 16 V (4S fully charged), 15 points from 30% "
                  "to 100% throttle. Motor surface temperature 80 C at 100% throttle after "
                  "10 min. Current and power are battery-side. Torque is published to two "
                  "decimals only, so it is used as a cross-check rather than a fitting "
                  "input."))]))

# ------------------------------------------------------------------ battery
lib.save("batteries", Battery(
    id="upgrade-energy-red-v4-4s2p-10000",
    name="Upgrade Energy RED V4 4S2P 10000mAh Amprius Li-ion",
    manufacturer="Upgrade Energy",
    chemistry="li-ion", cells_s=4, cells_p=2, capacity_mah=10000,
    weight_g=567, c_rating_cont=4.5, c_rating_burst=10.0, max_power_w=650,
    v_nominal_per_cell=3.6, v_max_per_cell=4.2, v_cutoff_per_cell=2.55,
    charge_current_a=10, discharge_connector="XT60", balance_connector="JST-XHP-5",
    dimensions_mm="145 x 43 x 43", cell_model="Amprius SA124 SiCore 5 Ah",
    internal_resistance_mohm_per_cell=12.0,
    provenance=Provenance(
        source_url="https://www.getfpv.com/upgrade-energy-red-v4-4s2p-10000mah-amprius-li-ion-battery-xt60.html",
        retrieved=RET, confidence="vendor",
        notes="4S2P, 10 Ah at the pack terminals, 144 Wh, 254 Wh/kg, 567 g. Min 10.2 V "
              "(2.55 V/cell), max 16.8 V (4.2 V/cell), charge 10 A. Continuous rating is "
              "quoted as 650 W sustained, which is 45 A at the 14.4 V nominal - that is "
              "where the 4.5C figure comes from; burst is 100 A (10C). INTERNAL RESISTANCE "
              "IS ESTIMATED at 12 mOhm/cell - not published. Measure it if you can, it "
              "drives sag and the end of the discharge. Silicon-anode Li-ion, so the model "
              "uses a lithium-ion open-circuit curve, not a LiPo one.")))

# ------------------------------------------------------------------ payload
lib.save("payloads", Payload(
    id="x500v2-avionics", name="X500 V2 avionics (FC + GPS + telemetry)",
    manufacturer="Holybro",
    provenance=Provenance(
        source_url="https://holybro.com/products/px4-development-kit-x500-v2",
        retrieved=RET, confidence="vendor",
        notes="Flight-critical electronics from the X500 V2 development kit. Masses are "
              "vendor figures. POWER FIGURES ARE ESTIMATES - measure yours with a clamp "
              "meter, they come straight off the flight pack. Add your sensors, compute and "
              "printed parts as further items."),
    items=[
        PayloadItem(name="Pixhawk 6C flight controller", mass_g=35, power_w=2.0,
                    provenance=Provenance(confidence="estimated", retrieved=RET,
                                          notes="Mass vendor; ~2 W typical for the FC alone.")),
        PayloadItem(name="M9N GPS + compass", mass_g=32, power_w=0.6,
                    provenance=Provenance(confidence="estimated", retrieved=RET)),
        PayloadItem(name="Telemetry radio (915 MHz)", mass_g=20, power_w=0.9,
                    provenance=Provenance(confidence="estimated", retrieved=RET,
                                          notes="Duty-cycle averaged; peaks higher on transmit.")),
        PayloadItem(name="PM02 power module", mass_g=28, power_w=0.4,
                    provenance=Provenance(confidence="estimated", retrieved=RET)),
    ]))

# -------------------------------------------------------------------- setup
lib.save("setups", Setup(
    id="x500v2-2216-default", name="Holybro X500 V2 (2216 920KV) + RED V4 10Ah",
    description="Holybro X500 V2 airframe, 4x 2216 920KV on T1045II propellers, BLHeli_S 20A, "
                "carrying the Upgrade Energy RED V4 4S2P 10 Ah Amprius pack. Avionics only - "
                "add your sensors and compute to the payload profile.",
    n_rotors=4, frame_id="x500-v2", motor_id="holybro-2216-920kv", prop_id="t1045ii",
    esc_id="blheli-s-20a", battery_id="upgrade-energy-red-v4-4s2p-10000",
    payload_id="x500v2-avionics", misc_mass_g=80, assumptions=Assumptions()))

print("seeded:")
for kind in ("frames", "props", "escs", "motors", "batteries", "payloads", "setups"):
    print(f"  {kind:10s} {len(lib.list_ids(kind))}  {', '.join(lib.list_ids(kind))}")
