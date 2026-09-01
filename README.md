# Daedalus Calculator

Multirotor propulsion design and tuning.

A tool for designing a multirotor and tuning its propulsion configuration: save component
profiles from datasheets, mix and match them into named setups, and see every metric that
matters for the trade between **flight time**, **efficiency** and **thrust-to-weight**.

The battery powers the payload as well as the motors, and the tool accounts for that
explicitly — the cost of *carrying* a payload and the cost of *powering* it are reported
separately.

```bash
./run.sh          # creates .venv on first run, then opens the app
```

Or manually:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/streamlit run app/Home.py
```

---

## Design ideology

The engine implements the design loop from Tyto Robotics' *Drone Design — Calculations and
Assumptions* and *How to Increase Drone Flight Time and Lift Capacity*:

> assume mass → derive hover thrust per rotor → pick the propeller most efficient **at that
> thrust** → read its torque/speed operating point → pick the motor most efficient **at that
> operating point** → size the ESC with headroom → size the battery → recompute mass → iterate.

Principles carried into the code:

| Principle | Where it lives |
|---|---|
| Compare efficiency **at the hover thrust point**, not at peak | every ranking is done at the solved hover thrust |
| Keep **≥ 2× hover thrust** for control authority | `min_twr` in `optimize.search`; a margin bar on the dashboard |
| ESC is sized against **peak** current, not hover | margins check ratings; the ceiling search finds which binds first |
| Battery gains flatten — find the knee | `optimize.find_knee` marks it on the sweep |
| Bigger, slower propellers are more efficient | falls out of the physics; visible in the prop sweep |
| Trust **measured** thrust-stand data over theory | Tier A is the default path; Tier B is badged ESTIMATED everywhere |

---

## The model

### Two tiers

**Tier A — measured.** When the motor profile carries a thrust table for the chosen propeller,
that table *is* the model. It is not used as a throttle lookup, because a lookup only answers
questions at the voltage and weight it was measured at. Instead it is reduced back to physics:

```
C_T = T / (ρ n² D⁴)          C_Q = Q / (ρ n² D⁵)
```

Inside the measured range, thrust ↔ rpm uses monotone (PCHIP) interpolation so the data is
reproduced exactly; outside it, the fitted constant `C_T` extrapolates.

**Tier B — parametric.** With no measured table, momentum theory with a figure of merit:

```
P_ideal = T^1.5 / √(2 ρ A)        P_shaft = P_ideal / FM
```

Every Tier B result is badged ESTIMATED, and the optimizer excludes them by default.

### Recovering the electrical model

The aerodynamic half of a thrust table is voltage-independent. The electrical half is recovered
by a joint least-squares fit of three parameters against the measured current column:

```
Q          = C_Q ρ n² D⁵
I_motor    = Q / K_t + I_0            K_t = 60 / (2π Kv)
V_motor    = rpm / Kv + I_motor R_m
P_bus_pred = V_motor I_motor / η_esc
```

Fitting the electrical column rather than reading the torque column is deliberate: manufacturer
torque columns are often rounded to two decimals, which on a 2216-class motor is 15–30%
quantisation at low throttle. The torque column is kept as an independent cross-check instead.
On the seed datasheet this recovers bus power to **0.75% RMS** across all 15 rows, and the
fitted `C_Q` lands within 5.5% of the torque column.

Published resistance values for this motor class disagree badly between vendors (115 mΩ on the
T-Motor datasheet, 38 mΩ quoted for the Holybro part — almost certainly a phase-to-phase versus
per-phase convention mismatch), which is exactly why the fit exists. Datasheet values seed the
fit and bound it; they are never trusted blindly.

### Why hover power does not depend on pack voltage

Worth stating because it is not obvious. The mechanical demand fixes the motor's torque, speed,
current and terminal voltage; the ESC simply adjusts duty to deliver that from whatever bus it
has. Pack voltage therefore sets duty, pack current and sag — and the *ceiling*, since once duty
reaches 1.0 the motor cannot spin faster and available thrust begins to fall as the pack drains.

### Endurance

Integrated, not divided. The pack is marched down in depth-of-discharge steps: bus power is
roughly constant, so as open-circuit voltage falls the current rises, which deepens the IR sag,
which brings the loaded cutoff forward. That feedback is the dominant real mechanism behind the
Peukert-style capacity penalty, and it emerges from stepping the discharge rather than needing a
fudge exponent. The run ends at the first of: loaded cell cutoff, the depth-of-discharge limit,
or the point where full thrust can no longer hold hover.

The naive `usable Wh / bus W` figure is reported alongside, so results stay comparable to eCalc
and to Tyto's own formula.

### Payload on the flight pack

Payload power is a constant DC load through a regulator efficiency, added to the bus. It does
**not** change thrust demand — the payload's mass is already in all-up weight — but it drains the
pack, raises pack current and worsens sag. The dashboard splits the two costs:

- **carrying** the payload: what its mass costs in minutes
- **powering** the payload: what its watts cost in minutes

---

## Validation

Neither figure below was used to tune anything. The physics is driven entirely by the T-Motor
thrust table; these are Holybro's independent published claims for the same aircraft.

| Quantity | This model | Holybro publish | Error |
|---|---|---|---|
| Hover endurance, no mission payload, 4S 5000 mAh | **17.2 min** | ~18 min | −4.4% |
| Maximum payload at 70% throttle | **1569 g** | 1500 g | +4.6% |

Both are locked as regression tests. They run against the 4S 5000 mAh LiPo Holybro's figures
refer to, which lives in the test suite rather than the library — so the anchor holds no matter
which pack you actually fly. The suite also pins a full round-trip: every datasheet row, pushed
back through the solver at the table's own voltage, recovers that row's rpm to within 2% and its
battery current to within 5%.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./.venv/bin/python -m pytest tests/   # 31 tests
```

(The plugin-autoload flag is only needed on machines with a system ROS install, whose pytest
plugins fail to import here.)

---

## Known limitations

Read these before trusting a number.

- **Hover only.** No forward-flight or cruise model yet. `endurance.FlightSegment` is the
  extension point — a `CruiseSegment` slots in without touching the solver.
- **The duty-cycle / throttle figure is indicative, not measured.** The fit pins bus power
  tightly, but bus power is a *product* of motor voltage and current, and the data does not
  constrain the split between them. Trust thrust, rpm, current and power; treat the throttle
  percentage as a rough guide. The available-thrust ceiling deliberately does not depend on it —
  for Tier A it is anchored to the measured end of the table, scaled as `(V/V_test)²`.
- **The seed 2216 profile is a class-equivalent substitution.** Holybro publish only KV, size and
  weight for their 2216 920KV. The electrical parameters and thrust table come from the T-Motor
  AIR2216II KV920 datasheet — same stator, same KV, same 4S rating, same 1045 propeller. Treat
  the numbers as representative of the class, not as measured on your motors.
- **Avionics and payload power are estimates.** They come straight off the bus, so a clamp-meter
  reading of your actual draw will change endurance directly. They are flagged low-confidence
  until you replace them.
- **The RED V4 pack's internal resistance is an estimate.** 12 mΩ/cell is not published. It drives
  sag and how early the discharge ends, so measure it if you can. Everything else about that pack
  — 10 Ah, 144 Wh, 254 Wh/kg, 567 g, 650 W sustained, 100 A burst — is from the vendor and the
  model reproduces the published watt-hours and specific energy exactly.
- **No thermal model.** Margins compare against rated currents; they do not predict motor
  temperature. A motor sitting at 95% of its rating in still air is not fine just because the bar
  is not red.

---

## Provenance

Every profile records where its numbers came from:

| Level | Meaning |
|---|---|
| `measured` | you tested this exact hardware on a thrust stand |
| `datasheet` | manufacturer datasheet |
| `vendor` | vendor listing, not a full datasheet |
| `estimated` | guessed or derived from theory |

Provenance is editable like any other field, so as you replace estimates with measurements the
confidence follows.

The **weakest link** propagates: a setup is only as trustworthy as its least-supported input, and
the dashboard reports that level rather than the best one. This is why the stock X500 V2 setup
reads ESTIMATED overall despite its measured thrust table — the pack mass and avionics draw are
still guesses.

---

## What is in the library

Deliberately small — only components backed by a document, so nothing placeholder can quietly
end up in a result:

| | |
|---|---|
| Frame | Holybro X500 V2 |
| Motor | Holybro 2216 920KV, with the 15-point AIR2216II thrust table |
| Propeller | T1045II 10×4.5 |
| ESC | BLHeli_S 20A |
| Battery | Upgrade Energy RED V4 4S2P 10 Ah Amprius Li-ion |
| Payload | X500 V2 avionics (FC, GPS, telemetry, power module) |

Add your sensors, compute and printed parts as items on the payload profile — that is where the
mass and the watts that come off the flight pack belong.

## Editing and adding components

The **Components** page shows each profile as a grouped spec sheet. Click the pencil beside any
value to change it; the row becomes an input, and saving validates through the model before
writing to `data/`. Field constraints are real — a negative current, an efficiency above 1, or a
cutoff voltage above nominal is rejected rather than saved. A profile that will not load is
skipped with a banner rather than taking the whole library down.

The `id` is locked, since setups reference components by it.

For a motor you can paste a thrust table as CSV with columns
`throttle_pct, thrust_g, torque_nm, current_a, rpm, power_w`. The fitted `C_Q`, `R_m`, `I_0`,
`C_T` and the fit residual appear immediately, so you can tell straight away whether the table is
self-consistent.

Adding one measured table moves a motor from estimated momentum theory onto real data — it is
by far the highest-value thing you can do to this tool. Free sources of thrust-stand data:

- Tyto Robotics component database — <https://database.tytorobotics.com>
- manufacturer datasheets (T-Motor, SunnySky and others publish full tables)
- your own thrust stand, which beats all of the above

Profiles are plain JSON, one file per component under `data/`, so they can also be edited
directly or generated by a script. `scripts/seed_library.py` rebuilds the shipped library and is
a working example.

Current-rating convention, since it is a common trap: **motor and ESC amp ratings here are
battery-side**, matching how they are published and how thrust tables measure them. On the seed
datasheet the "17 A peak current" spec and the 16.37 A top row of the current column are plainly
the same quantity.

---

## Layout

```
dronecalc/
  models.py         component profiles, setups, provenance
  store.py          JSON library; resolve() hands out deep copies
  physics/
    propeller.py    Tier A table fit + Tier B momentum theory
    motor.py        Kv / Rm / Io model
    fit.py          joint (Cq, Rm, Io) recovery from a thrust table
    battery.py      OCV curve, sag, constant-power solve
    esc.py          efficiency, duty, limits
    atmosphere.py   density from altitude and temperature
  solver.py         the hover chain and the thrust ceiling
  endurance.py      discharge march; FlightSegment mission hook
  metrics.py        power budget, margins, sensitivity
  optimize.py       sweeps, knee finding, search, Pareto front
app/
  spec_ui.py        spec sheets with per-field inline editing
  Home.py + pages/  Streamlit UI
data/               the component library
tests/              31 tests, including the two validation targets
```

## Running it

Locally:

```bash
./run.sh
```

First run creates `.venv` and installs `requirements.txt`; after that it goes
straight to `streamlit run app/Home.py`.

## Storage backends

The library is addressed through `dronecalc/backends.py`, not the filesystem:

| backend | used when | notes |
| --- | --- | --- |
| `FileBackend` | default | one JSON file per profile under `data/<kind>/<id>.json` |
| `SupabaseBackend` | `SUPABASE_URL` and `SUPABASE_KEY` are both set | one Postgres row per profile, `(kind, id) -> jsonb` |

Selection happens in `default_backend()`. It is not a silent fallback: if the
URL is set but the connection fails, that is raised rather than quietly writing
to a local file the cloud will never see. Home's **Model provenance** panel
names the store actually in use, so a misconfigured deployment is visible rather
than merely wrong.

Profiles are stored whole, as JSONB, rather than shredded into columns. The
Pydantic models already are the schema and they change with every new datasheet
field; nothing queries inside a profile, because the app loads the library whole
and works in memory.

## Deploying

See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — Streamlit Community Cloud plus
Supabase, both free tier. `deploy/schema.sql` creates the table;
`scripts/sync_to_supabase.py` moves the library in either direction.

Access is a single shared team password (`app/auth.py`), checked against the
`app_password` secret. There are no user accounts, so there is no per-user
attribution or edit history — if you need to know who changed a component, that
is the piece to replace. With no password configured the app is open, and says
so in the sidebar.


## Sources

- [Tyto Robotics — Drone Design: Calculations and Assumptions](https://www.tytorobotics.com/blogs/articles/the-drone-design-loop-for-brushless-motors-and-propellers)
- [Tyto Robotics — How to Increase Drone Flight Time and Lift Capacity](https://www.tytorobotics.com/blogs/articles/how-to-increase-drone-flight-time-and-lift-capacity)
- [Tyto Robotics — How to Make a Drone Fly Longer](https://www.tytorobotics.com/blogs/articles/how-to-make-a-drone-fly-longer)
- [Tyto Robotics component database](https://database.tytorobotics.com/)
- [T-Motor Air Gear 450 II datasheet (AIR2216II KV920, AIR 20A, T1045)](https://cdn.robotshop.com/media/T/Tmo/RB-Tmo-266/pdf/ligpower_airgear_450ii_combo_set_multi_rotor_uav_p_datasheet.pdf)
- [Holybro X500 V2 kits](https://holybro.com/products/x500-v2-kits)
- [Upgrade Energy RED V4 4S2P 10000 mAh Amprius Li-ion](https://www.getfpv.com/upgrade-energy-red-v4-4s2p-10000mah-amprius-li-ion-battery-xt60.html)
- [eCalc xcopterCalc](https://www.ecalc.ch/xcoptercalc.php) — for cross-checking
