"""Air density from altitude and temperature.

Density matters more than people expect: hover power scales as 1/sqrt(rho), so a
2000 m field on a hot day costs real flight time.
"""

from __future__ import annotations

#: Sea-level ISA values.
RHO_SL = 1.225  # kg/m^3
P_SL = 101325.0  # Pa
T_SL = 288.15  # K
LAPSE = 0.0065  # K/m
R_AIR = 287.058  # J/(kg K)
G0 = 9.80665  # m/s^2

#: Standard gravity used throughout for gram <-> newton conversion.
G = 9.80665


def isa_pressure(altitude_m: float) -> float:
    """ISA static pressure at `altitude_m` (troposphere)."""
    return P_SL * (1.0 - LAPSE * altitude_m / T_SL) ** (G0 / (R_AIR * LAPSE))


def air_density(altitude_m: float = 0.0, temperature_c: float | None = None) -> float:
    """Air density in kg/m^3.

    Pressure follows the ISA lapse; temperature is the actual (possibly
    non-standard) ambient value, which is what makes hot-and-high cases work.
    """
    pressure = isa_pressure(altitude_m)
    if temperature_c is None:
        temp_k = T_SL - LAPSE * altitude_m
    else:
        temp_k = temperature_c + 273.15
    if temp_k <= 0:
        raise ValueError("temperature must be above absolute zero")
    return pressure / (R_AIR * temp_k)


def speed_of_sound(temperature_c: float) -> float:
    """Speed of sound in m/s, used for the propeller tip Mach check."""
    return (1.4 * R_AIR * (temperature_c + 273.15)) ** 0.5


def grams_to_newtons(grams: float) -> float:
    return grams * G / 1000.0


def newtons_to_grams(newtons: float) -> float:
    return newtons * 1000.0 / G
