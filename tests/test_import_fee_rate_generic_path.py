"""Real regression test, found live on devhub (2026-08-24, nimbus repo
issue #152): TOU network fees and the flat fee rate (import_fee_rate(),
solver_flat_fee_rate) used to be applied ONLY inside the has_localvolts
branch of main()'s price-building block, even though those are genuinely
generic, portable config-flow fields (number.nimbus_solver_network_fee_*,
number.nimbus_solver_flat_fee_rate) with no LocalVolts dependency at all.
Any real installer without LocalVolts who filled them in via the wizard
(exactly as it invites them to) had them silently ignored -- zero fee
ever applied, zero warning, zero diagnostic signal.

This test covers import_fee_rate() itself (the actual block-selection
logic) -- it was already correct before this fix; what changed is WHERE
it gets called from (moved out of the has_localvolts-only branch to
apply uniformly to both paths). See solver_writer.py's own inline
comments at the fix site for the structural change.
"""

import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestImportFeeRateBlockSelection(unittest.TestCase):
    def test_default_rate_when_no_blocks_configured(self):
        cfg = {"solver_network_fee_default_rate": 0.05}
        self.assertEqual(solver_writer.import_fee_rate(cfg, 14), 0.05)

    def test_block_1_applies_within_its_hour_range(self):
        cfg = {
            "solver_network_fee_default_rate": 0.05,
            "solver_network_fee_1_rate": 0.21,
            "solver_network_fee_1_start_hour": 16,
            "solver_network_fee_1_end_hour": 21,
        }
        self.assertEqual(solver_writer.import_fee_rate(cfg, 16), 0.21)
        self.assertEqual(solver_writer.import_fee_rate(cfg, 20), 0.21)

    def test_block_1_does_not_apply_outside_its_hour_range(self):
        cfg = {
            "solver_network_fee_default_rate": 0.05,
            "solver_network_fee_1_rate": 0.21,
            "solver_network_fee_1_start_hour": 16,
            "solver_network_fee_1_end_hour": 21,
        }
        # Exactly the real devhub repro: hour 22, outside block 1
        # (16-21), must fall through to the default rate, not 0.
        self.assertEqual(solver_writer.import_fee_rate(cfg, 22), 0.05)
        self.assertEqual(solver_writer.import_fee_rate(cfg, 10), 0.05)

    def test_zero_rate_block_is_treated_as_unconfigured(self):
        cfg = {
            "solver_network_fee_default_rate": 0.05,
            "solver_network_fee_1_rate": 0.0,
            "solver_network_fee_1_start_hour": 16,
            "solver_network_fee_1_end_hour": 21,
        }
        self.assertEqual(solver_writer.import_fee_rate(cfg, 18), 0.05)

    def test_everything_unconfigured_is_a_complete_no_op(self):
        self.assertEqual(solver_writer.import_fee_rate({}, 12), 0.0)


if __name__ == "__main__":
    unittest.main()
