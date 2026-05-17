"""Tests for AbsolutePosition geometry: haversine, ECEF, ENU projection."""
from __future__ import annotations

import pytest

from aetherward.position.absolute import AbsolutePosition, FixType


class TestAbsolutePosition:
    def _pos(self, lat, lon, alt=0.0, fix=FixType.FIX_3D):
        return AbsolutePosition(lat=lat, lon=lon, alt=alt, fix_type=fix)

    def test_is_valid_with_fix(self):
        p = self._pos(48.0, 2.0)
        assert p.is_valid()

    def test_is_valid_without_fix(self):
        p = self._pos(48.0, 2.0, fix=FixType.NONE)
        assert not p.is_valid()

    def test_distance_to_self_is_zero(self):
        p = self._pos(48.0, 2.0)
        assert p.distance_to(p) == pytest.approx(0.0, abs=1e-3)

    def test_distance_equator_1deg_lon(self):
        # At the equator, 1° lon ≈ 111 195 m (haversine on a sphere of R=6 371 000)
        a = self._pos(0.0, 0.0)
        b = self._pos(0.0, 1.0)
        d = a.distance_to(b)
        assert d == pytest.approx(111_195.0, rel=0.005)

    def test_distance_symmetry(self):
        a = self._pos(48.8566, 2.3522)
        b = self._pos(51.5074, -0.1278)
        assert a.distance_to(b) == pytest.approx(b.distance_to(a), rel=1e-6)

    def test_distance_paris_london(self):
        # Paris to London ≈ 340 km
        paris  = self._pos(48.8566,  2.3522)
        london = self._pos(51.5074, -0.1278)
        assert paris.distance_to(london) == pytest.approx(340_000, rel=0.05)

    def test_ecef_roundtrip(self):
        orig = self._pos(48.8566, 2.3522, alt=35.0)
        x, y, z = orig.to_ecef()
        recovered = AbsolutePosition.from_ecef(x, y, z)
        assert recovered.lat == pytest.approx(orig.lat, abs=1e-7)
        assert recovered.lon == pytest.approx(orig.lon, abs=1e-7)

    def test_ecef_equator(self):
        # At equator, lon=0: ECEF x ≈ R+alt, y ≈ 0, z ≈ 0
        p = self._pos(0.0, 0.0, alt=0.0)
        x, y, z = p.to_ecef()
        assert x == pytest.approx(6_378_137.0, rel=1e-4)
        assert abs(y) < 1.0
        assert abs(z) < 1.0

    def test_to_enu_self_is_origin(self):
        p = self._pos(48.0, 2.0)
        e, n, u = p.to_enu(p)
        assert e == pytest.approx(0.0, abs=0.01)
        assert n == pytest.approx(0.0, abs=0.01)
        assert u == pytest.approx(0.0, abs=0.01)

    def test_to_enu_north(self):
        origin = self._pos(48.0, 2.0)
        north  = self._pos(48.001, 2.0)
        e, n, u = north.to_enu(origin)
        assert abs(e) < 1.0          # almost no east component
        assert n == pytest.approx(111.3, abs=2.0)  # ~111 m per 0.001°

    def test_to_enu_east(self):
        origin = self._pos(48.0, 2.0)
        east   = self._pos(48.0, 2.001)
        e, n, u = east.to_enu(origin)
        assert abs(n) < 1.0
        assert e > 50.0  # positive east component

    def test_repr(self):
        p = self._pos(48.8566, 2.3522)
        r = repr(p)
        assert '48.856600' in r
        assert '2.352200' in r

    def test_fix_types(self):
        for ft in FixType:
            p = AbsolutePosition(lat=0, lon=0, fix_type=ft)
            assert p.fix_type == ft
