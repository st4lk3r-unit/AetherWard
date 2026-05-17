from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

import numpy as np


class OriSource(IntEnum):
    IMU     = 0
    COMPASS = 1
    MANUAL  = 2


@dataclass
class Orientation:
    """
    Unit quaternion (w, x, y, z) representing rotation from body frame
    to local ENU frame.  Used for antenna pointing and array heading.
    """
    w: float = 1.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    accuracy: float = float('inf')    # estimated error, degrees
    timestamp: float = 0.0
    source: OriSource = OriSource.MANUAL

    def normalize(self) -> Orientation:
        n = math.sqrt(self.w**2 + self.x**2 + self.y**2 + self.z**2)
        if n < 1e-12:
            return Orientation.identity()
        return Orientation(self.w/n, self.x/n, self.y/n, self.z/n,
                           self.accuracy, self.timestamp, self.source)

    def to_rotation_matrix(self) -> np.ndarray:
        w, x, y, z = self.w, self.x, self.y, self.z
        return np.array([
            [1-2*(y*y+z*z),   2*(x*y-w*z),   2*(x*z+w*y)],
            [  2*(x*y+w*z), 1-2*(x*x+z*z),   2*(y*z-w*x)],
            [  2*(x*z-w*y),   2*(y*z+w*x), 1-2*(x*x+y*y)],
        ], dtype=np.float64)

    def rotate_vector(self, v: np.ndarray) -> np.ndarray:
        return self.to_rotation_matrix() @ np.asarray(v, dtype=np.float64)

    def conjugate(self) -> Orientation:
        return Orientation(self.w, -self.x, -self.y, -self.z,
                           self.accuracy, self.timestamp, self.source)

    def __mul__(self, other: Orientation) -> Orientation:
        """Hamilton product — compose two rotations."""
        w1, x1, y1, z1 = self.w, self.x, self.y, self.z
        w2, x2, y2, z2 = other.w, other.x, other.y, other.z
        return Orientation(
            w = w1*w2 - x1*x2 - y1*y2 - z1*z2,
            x = w1*x2 + x1*w2 + y1*z2 - z1*y2,
            y = w1*y2 - x1*z2 + y1*w2 + z1*x2,
            z = w1*z2 + x1*y2 - y1*x2 + z1*w2,
        )

    @classmethod
    def identity(cls) -> Orientation:
        return cls(w=1.0, x=0.0, y=0.0, z=0.0)

    @classmethod
    def from_euler(cls, roll: float, pitch: float, yaw: float,
                   degrees: bool = True, **kwargs) -> Orientation:
        """ZYX Euler angles (yaw → pitch → roll) to quaternion."""
        if degrees:
            roll  = math.radians(roll)
            pitch = math.radians(pitch)
            yaw   = math.radians(yaw)
        cr, sr = math.cos(roll/2),  math.sin(roll/2)
        cp, sp = math.cos(pitch/2), math.sin(pitch/2)
        cy, sy = math.cos(yaw/2),   math.sin(yaw/2)
        return cls(
            w = cr*cp*cy + sr*sp*sy,
            x = sr*cp*cy - cr*sp*sy,
            y = cr*sp*cy + sr*cp*sy,
            z = cr*cp*sy - sr*sp*cy,
            **kwargs,
        )

    @classmethod
    def from_axis_angle(cls, axis: np.ndarray, angle_rad: float, **kwargs) -> Orientation:
        axis = np.asarray(axis, dtype=np.float64)
        axis = axis / np.linalg.norm(axis)
        s = math.sin(angle_rad / 2)
        return cls(
            w = math.cos(angle_rad / 2),
            x = float(axis[0] * s),
            y = float(axis[1] * s),
            z = float(axis[2] * s),
            **kwargs,
        )

    def to_euler(self, degrees: bool = True) -> tuple[float, float, float]:
        """Return (roll, pitch, yaw) in ZYX convention."""
        w, x, y, z = self.w, self.x, self.y, self.z
        roll  = math.atan2(2*(w*x + y*z), 1 - 2*(x*x + y*y))
        pitch = math.asin(max(-1.0, min(1.0, 2*(w*y - z*x))))
        yaw   = math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
        if degrees:
            return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)
        return roll, pitch, yaw

    def __repr__(self) -> str:
        r, p, ya = self.to_euler()
        return f"Orientation(roll={r:.1f}°, pitch={p:.1f}°, yaw={ya:.1f}°, src={self.source.name})"
