"""Unit tests for CFFET FFET-inspired NPNP tier utilization."""

import unittest
from unittest.mock import MagicMock

from src.cellgen.archit import config as archit_config
from src.cellgen.archit.CFFET.tier_utilization import _block_tiers
from src.cellgen.core.entity import Circuit, Transistor


def _add_tran(circuit, tran):
    circuit.transistors[tran.name] = tran
    for pin, net_name in (
        ("source", tran.source),
        ("gate", tran.gate),
        ("drain", tran.drain),
    ):
        net = circuit.add_net(net_name)
        net.add_connection(tran.name, pin)


def _make_tran(name, source, gate, drain, model):
    return Transistor(
        name, source, gate, drain, "VDD", model, "w=1", "l=1", "nfin=1"
    )


class TestNpvpBlockTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archit_config.init()

    def test_p_on_n_block_mapping(self):
        inst = MagicMock()
        inst.c_tech = MagicMock()
        inst.c_tech.get_bpc_tiers = lambda: ("BBOTPC", "FBOTPC")
        inst.c_tech.get_pc_tiers = lambda: ("BTOPPC", "FTOPPC")
        (back_nm, back_pm), (front_nm, front_pm) = _block_tiers(inst)
        self.assertEqual(back_nm, "BBOTPC")
        self.assertEqual(back_pm, "BTOPPC")
        self.assertEqual(front_nm, "FBOTPC")
        self.assertEqual(front_pm, "FTOPPC")


class TestFfetGatePolarity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archit_config.init()

    def _polarity(self, circuit, net_name):
        from src.cellgen.archit.CFFET.main import CFFET

        inst = MagicMock()
        inst.circuit = circuit
        return CFFET._cffet_gate_polarity(inst, net_name)

    def test_nmos_only_input(self):
        circuit = Circuit()
        _add_tran(circuit, _make_tran("MN0", "Y", "CLK", "VSS", "nmos"))
        self.assertEqual(self._polarity(circuit, "CLK"), "back")

    def test_pmos_only_input(self):
        circuit = Circuit()
        _add_tran(circuit, _make_tran("MP0", "VDD", "EN", "Y", "pmos"))
        self.assertEqual(self._polarity(circuit, "EN"), "front")

    def test_mixed_cmos_input(self):
        circuit = Circuit()
        _add_tran(circuit, _make_tran("MP0", "Y", "A", "VDD", "pmos"))
        _add_tran(circuit, _make_tran("MN0", "Y", "A", "VSS", "nmos"))
        self.assertIsNone(self._polarity(circuit, "A"))


if __name__ == "__main__":
    unittest.main()
