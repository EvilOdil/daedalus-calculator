"""ESC model: conversion efficiency, duty cycle and current limits."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ESC


@dataclass(frozen=True)
class ESCState:
    bus_power_w: float
    bus_current_a: float
    duty: float
    loss_w: float


@dataclass(frozen=True)
class ESCModel:
    efficiency: float
    resistance_ohm: float = 0.0

    @classmethod
    def from_profile(cls, esc: ESC) -> "ESCModel":
        r = (esc.resistance_mohm or 0.0) / 1000.0
        return cls(efficiency=esc.efficiency, resistance_ohm=r)

    def solve(self, motor_power_w: float, motor_voltage_v: float, bus_voltage_v: float) -> ESCState:
        """Bus-side draw for a given motor-side demand.

        Duty is the ratio of motor terminal voltage to bus voltage, which is the
        quantity a throttle command actually sets.
        """
        if bus_voltage_v <= 0:
            raise ValueError("bus voltage must be positive")
        bus_power = motor_power_w / self.efficiency
        bus_current = bus_power / bus_voltage_v
        duty = motor_voltage_v / bus_voltage_v
        return ESCState(
            bus_power_w=bus_power,
            bus_current_a=bus_current,
            duty=duty,
            loss_w=bus_power - motor_power_w,
        )
