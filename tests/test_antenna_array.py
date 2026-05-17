"""
Tests for AntennaArray: O(1) get(), add(), remove(), __post_init__ index, n property.

Also covers resolve_antenna_absolute() coordinate projection and geometry_matrix().
"""
from __future__ import annotations

import pytest

from aetherward.antenna.antenna import Antenna
from aetherward.antenna.array import AntennaArray
from aetherward.orientation.quaternion import Orientation
from aetherward.position.absolute import AbsolutePosition, FixType
from aetherward.position.relative import RelativePosition


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ant(ant_id: str, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Antenna:
    return Antenna(
        id=ant_id,
        position=RelativePosition(x=x, y=y, z=z),
        orientation=Orientation.identity(),
    )


def _arr(*ant_ids: str) -> AntennaArray:
    arr = AntennaArray(id='test')
    for aid in ant_ids:
        arr.add(_ant(aid))
    return arr


# ── __post_init__ index ───────────────────────────────────────────────────────

class TestPostInitIndex:
    def test_empty_array_has_empty_index(self):
        arr = AntennaArray(id='empty')
        assert arr._by_id == {}

    def test_pre_populated_antennas_list_builds_index(self):
        """AntennaArray(antennas=[...]) builds _by_id from the existing list."""
        ants = [_ant('a0'), _ant('a1'), _ant('a2')]
        arr  = AntennaArray(id='prepop', antennas=ants)
        assert set(arr._by_id.keys()) == {'a0', 'a1', 'a2'}
        assert arr._by_id['a1'] is ants[1]

    def test_index_consistent_with_antennas_list(self):
        ants = [_ant(f'ant{i}') for i in range(5)]
        arr  = AntennaArray(id='consistency', antennas=ants)
        for ant in ants:
            assert arr._by_id[ant.id] is ant


# ── get() ─────────────────────────────────────────────────────────────────────

class TestGet:
    def test_get_returns_correct_antenna(self):
        arr = _arr('ant0', 'ant1', 'ant2')
        ant = arr.get('ant1')
        assert ant is not None
        assert ant.id == 'ant1'

    def test_get_unknown_id_returns_none(self):
        arr = _arr('ant0', 'ant1')
        assert arr.get('ghost') is None

    def test_get_empty_array_returns_none(self):
        arr = AntennaArray(id='empty')
        assert arr.get('anything') is None

    def test_get_first_antenna(self):
        arr = _arr('a', 'b', 'c')
        assert arr.get('a').id == 'a'

    def test_get_last_antenna(self):
        arr = _arr('a', 'b', 'c')
        assert arr.get('c').id == 'c'

    def test_get_correct_for_large_array(self):
        """get() uses the dict index; verify correctness across 20 entries."""
        arr = AntennaArray(id='order')
        for i in range(20):
            arr.add(_ant(f'ant{i:02d}', x=float(i)))
        for i in range(20):
            found = arr.get(f'ant{i:02d}')
            assert found is not None
            assert found.position.x == pytest.approx(float(i))


# ── add() ─────────────────────────────────────────────────────────────────────

class TestAdd:
    def test_add_appends_to_antennas_list(self):
        arr = AntennaArray(id='add')
        arr.add(_ant('x'))
        assert len(arr.antennas) == 1
        assert arr.antennas[0].id == 'x'

    def test_add_updates_index(self):
        arr = AntennaArray(id='add')
        ant = _ant('y')
        arr.add(ant)
        assert arr._by_id.get('y') is ant

    def test_add_multiple_antennas(self):
        arr = AntennaArray(id='multi')
        for i in range(10):
            arr.add(_ant(f'a{i}'))
        assert arr.n == 10
        assert len(arr._by_id) == 10
        for i in range(10):
            assert arr.get(f'a{i}') is not None

    def test_add_overrides_duplicate_id_in_index(self):
        """Adding an antenna with a duplicate ID replaces the index entry."""
        arr = AntennaArray(id='dup')
        old = _ant('dup_id', x=0.0)
        new = _ant('dup_id', x=99.0)
        arr.add(old)
        arr.add(new)
        assert arr._by_id['dup_id'] is new
        assert arr._by_id['dup_id'].position.x == pytest.approx(99.0)


# ── remove() ─────────────────────────────────────────────────────────────────

class TestRemove:
    def test_remove_deletes_from_list_and_index(self):
        arr = _arr('a', 'b', 'c')
        arr.remove('b')
        assert arr.get('b') is None
        assert all(a.id != 'b' for a in arr.antennas)

    def test_remove_decrements_n(self):
        arr = _arr('a', 'b', 'c')
        arr.remove('a')
        assert arr.n == 2

    def test_remove_nonexistent_id_is_noop(self):
        arr = _arr('a', 'b')
        arr.remove('ghost')   # must not raise
        assert arr.n == 2

    def test_remove_last_antenna(self):
        arr = _arr('only')
        arr.remove('only')
        assert arr.n == 0
        assert arr.get('only') is None

    def test_remove_preserves_remaining_antennas(self):
        arr = _arr('x', 'y', 'z')
        arr.remove('y')
        assert arr.get('x') is not None
        assert arr.get('z') is not None
        assert arr.n == 2

    def test_add_after_remove(self):
        arr = _arr('a', 'b')
        arr.remove('a')
        arr.add(_ant('a', x=5.0))
        assert arr.get('a').position.x == pytest.approx(5.0)


# ── n property ────────────────────────────────────────────────────────────────

class TestNProperty:
    def test_n_zero_for_empty(self):
        assert AntennaArray(id='empty').n == 0

    def test_n_increments_on_add(self):
        arr = AntennaArray(id='cnt')
        for i in range(7):
            arr.add(_ant(f'a{i}'))
            assert arr.n == i + 1

    def test_n_decrements_on_remove(self):
        arr = _arr('a', 'b', 'c', 'd')
        arr.remove('b')
        assert arr.n == 3


# ── Live updates ──────────────────────────────────────────────────────────────

class TestLiveUpdates:
    def test_update_position_sets_absolute(self):
        arr = AntennaArray(id='live')
        pos = AbsolutePosition(lat=48.0, lon=2.0, fix_type=FixType.FIX_3D)
        arr.update_position(pos)
        assert arr.absolute_position is pos

    def test_update_orientation_sets_imu(self):
        arr = AntennaArray(id='live')
        ori = Orientation.from_euler(0, 0, 45)
        arr.update_orientation(ori)
        assert arr.orientation is ori

    def test_repr_reflects_gps_and_imu(self):
        arr = _arr('a')
        assert 'gps=no' in repr(arr)
        assert 'imu=no' in repr(arr)
        arr.update_position(AbsolutePosition(lat=0, lon=0, fix_type=FixType.FIX_3D))
        arr.update_orientation(Orientation.identity())
        assert 'gps=yes' in repr(arr)
        assert 'imu=yes' in repr(arr)


# ── geometry_matrix() ─────────────────────────────────────────────────────────

class TestGeometryMatrix:
    def test_shape_is_n_by_3(self):
        arr = AntennaArray(id='geom')
        for i in range(4):
            arr.add(_ant(f'a{i}', x=float(i), y=float(i)*2, z=0.0))
        G = arr.geometry_matrix()
        assert G.shape == (4, 3)

    def test_values_match_positions(self):
        arr = AntennaArray(id='geom')
        coords = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        for i, (x, y, z) in enumerate(coords):
            arr.add(_ant(f'a{i}', x=x, y=y, z=z))
        G = arr.geometry_matrix()
        for i, (x, y, z) in enumerate(coords):
            assert G[i, 0] == pytest.approx(x)
            assert G[i, 1] == pytest.approx(y)
            assert G[i, 2] == pytest.approx(z)

    def test_empty_array_returns_empty_matrix(self):
        G = AntennaArray(id='empty').geometry_matrix()
        assert G.ndim == 2
        assert G.shape == (0, 3)


# ── resolve_antenna_absolute() ────────────────────────────────────────────────

class TestResolveAntennaAbsolute:
    def test_no_gps_returns_none(self):
        arr = _arr('a0')
        assert arr.resolve_antenna_absolute('a0') is None

    def test_unknown_antenna_returns_none(self):
        arr = _arr('a0')
        arr.update_position(AbsolutePosition(lat=48.0, lon=2.0,
                                              fix_type=FixType.FIX_3D))
        assert arr.resolve_antenna_absolute('ghost') is None

    def test_origin_antenna_matches_gps_anchor(self):
        """An antenna at (0,0,0) should project to the GPS anchor position."""
        arr = AntennaArray(id='proj')
        arr.add(_ant('ref', x=0.0, y=0.0, z=0.0))
        gps = AbsolutePosition(lat=48.8566, lon=2.3522, alt=35.0,
                                fix_type=FixType.FIX_3D)
        arr.update_position(gps)

        abs_pos = arr.resolve_antenna_absolute('ref')
        assert abs_pos is not None
        assert abs_pos.lat == pytest.approx(48.8566, abs=1e-5)
        assert abs_pos.lon == pytest.approx(2.3522,  abs=1e-5)

    def test_offset_antenna_differs_from_anchor(self):
        """An antenna offset 100 m east should differ from the anchor."""
        arr = AntennaArray(id='proj')
        arr.add(_ant('east', x=100.0, y=0.0, z=0.0))
        gps = AbsolutePosition(lat=48.8566, lon=2.3522, alt=0.0,
                                fix_type=FixType.FIX_3D)
        arr.update_position(gps)

        abs_pos = arr.resolve_antenna_absolute('east')
        assert abs_pos is not None
        assert abs(abs_pos.lon - 2.3522) > 0.0005   # longitude shifted east
