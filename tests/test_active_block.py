"""active_block() bisect implementation must match the old linear scan exactly."""
from __future__ import annotations

import unittest

from adm_player.adm_model import ObjectBlock, ObjectPosition, active_block


def _pos() -> ObjectPosition:
    return ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0)


def _blocks(spans: list[tuple[float, float]]) -> list[ObjectBlock]:
    bs = [ObjectBlock(start_sec=s, end_sec=e, position=_pos()) for s, e in spans]
    bs.sort(key=lambda b: b.start_sec)  # parse_adm_objects guarantees this order
    return bs


def _linear_reference(blocks: list[ObjectBlock], t: float) -> ObjectBlock | None:
    """The pre-optimization implementation, kept here as the oracle."""
    best: ObjectBlock | None = None
    for b in blocks:
        if b.start_sec <= t < b.end_sec:
            if best is None or b.start_sec >= best.start_sec:
                best = b
    return best


class TestActiveBlock(unittest.TestCase):
    def test_matches_linear_scan_across_layouts(self) -> None:
        layouts = [
            [],                                    # empty
            [(0.0, 1.0)],                          # single
            [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)],  # contiguous (common case)
            [(0.0, 1.0), (2.0, 3.0)],              # gap between blocks
            [(0.0, 2.0), (1.0, 3.0)],              # overlap
            [(0.0, 5.0), (1.0, 1.5), (2.0, 2.5)],  # long block spanning short ones
            [(1.0, 2.0), (1.0, 3.0)],              # duplicate start_sec
        ]
        times = [-1.0, 0.0, 0.5, 1.0, 1.5, 1.7, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0]
        for spans in layouts:
            blocks = _blocks(spans)
            for t in times:
                got = active_block(blocks, t)
                exp = _linear_reference(blocks, t)
                # Identity, not equality: must pick the *same* block object.
                self.assertIs(got, exp, msg=f"layout={spans} t={t}")

    def test_half_open_interval(self) -> None:
        blocks = _blocks([(0.0, 1.0)])
        self.assertIsNotNone(active_block(blocks, 0.0))  # start inclusive
        self.assertIsNone(active_block(blocks, 1.0))     # end exclusive

    def test_empty(self) -> None:
        self.assertIsNone(active_block([], 0.0))


if __name__ == "__main__":
    unittest.main()
