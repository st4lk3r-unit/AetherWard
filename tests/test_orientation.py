"""Tests for Orientation (quaternion) math."""
from __future__ import annotations

import math

import numpy as np
import pytest

from aetherward.orientation.quaternion import Orientation, OriSource


class TestOrientation:
    def test_identity_default(self):
        o = Orientation()
        assert o.w == pytest.approx(1.0)
        assert o.x == pytest.approx(0.0)
        assert o.y == pytest.approx(0.0)
        assert o.z == pytest.approx(0.0)

    def test_normalize_unit_is_unchanged(self):
        o = Orientation(w=1.0, x=0.0, y=0.0, z=0.0)
        n = o.normalize()
        assert n.w == pytest.approx(1.0)

    def test_normalize_arbitrary(self):
        o = Orientation(w=2.0, x=0.0, y=0.0, z=0.0)
        n = o.normalize()
        assert n.w == pytest.approx(1.0)
        mag = math.sqrt(n.w**2 + n.x**2 + n.y**2 + n.z**2)
        assert mag == pytest.approx(1.0)

    def test_normalize_zero_returns_identity(self):
        o = Orientation(w=0.0, x=0.0, y=0.0, z=0.0)
        n = o.normalize()
        assert n.w == pytest.approx(1.0)

    def test_rotation_matrix_identity(self):
        o = Orientation(w=1.0)
        R = o.to_rotation_matrix()
        assert R.shape == (3, 3)
        assert np.allclose(R, np.eye(3))

    def test_rotate_vector_identity(self):
        o = Orientation(w=1.0)
        v = np.array([1.0, 0.0, 0.0])
        rv = o.rotate_vector(v)
        assert np.allclose(rv, v)

    def test_rotate_vector_90deg_z(self):
        # 90° rotation around Z: x→y, y→-x
        angle = math.pi / 2
        o = Orientation(w=math.cos(angle/2), x=0, y=0, z=math.sin(angle/2)).normalize()
        v = np.array([1.0, 0.0, 0.0])
        rv = o.rotate_vector(v)
        assert np.allclose(rv, [0.0, 1.0, 0.0], atol=1e-10)

    def test_conjugate_reverses_rotation(self):
        angle = math.pi / 3
        o = Orientation(w=math.cos(angle/2), x=math.sin(angle/2), y=0, z=0).normalize()
        v = np.array([0.0, 1.0, 0.0])
        rv  = o.rotate_vector(v)
        rev = o.conjugate().rotate_vector(rv)
        assert np.allclose(rev, v, atol=1e-10)

    def test_from_euler_zero(self):
        o = Orientation.from_euler(0.0, 0.0, 0.0)
        assert o.w == pytest.approx(1.0, abs=1e-10)
        assert abs(o.x) < 1e-10
        assert abs(o.y) < 1e-10
        assert abs(o.z) < 1e-10

    def test_from_euler_yaw_90(self):
        o = Orientation.from_euler(0.0, 0.0, 90.0)
        v = np.array([1.0, 0.0, 0.0])
        rv = o.rotate_vector(v)
        assert np.allclose(rv, [0.0, 1.0, 0.0], atol=1e-10)

    def test_source_field(self):
        o = Orientation(source=OriSource.IMU)
        assert o.source == OriSource.IMU

    def test_identity_classmethod(self):
        o = Orientation.identity()
        assert o.w == pytest.approx(1.0)
        R = o.to_rotation_matrix()
        assert np.allclose(R, np.eye(3))
