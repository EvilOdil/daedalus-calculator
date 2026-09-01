"""Drone propulsion design and tuning calculator.

Implements the Tyto Robotics drone design loop: derive hover thrust from mass,
select the propeller most efficient at that thrust, match a motor to the
resulting torque/speed operating point, size the ESC with headroom, size the
battery, then iterate.
"""

__version__ = "0.1.0"
