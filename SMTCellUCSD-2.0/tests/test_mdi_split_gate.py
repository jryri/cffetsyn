"""Unit tests for CFFET MDI split-gate pair detection."""

import unittest

from src.cellgen.archit import config as archit_config
from src.cellgen.archit.CFFET.mdi_split_gate import (
    detect_tg_split_gate_pairs,
    gates_are_complementary,
)
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


class TestMdiSplitGateDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archit_config.init()

    def test_detects_complementary_tg_pair(self):
        circuit = Circuit()
        _add_tran(
            circuit,
            _make_tran("MP_PASS", "CH", "SN", "CH", "pmos"),
        )
        _add_tran(
            circuit,
            _make_tran("MN_PASS", "CH", "S", "CH", "nmos"),
        )

        pairs = detect_tg_split_gate_pairs(circuit)
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["pmos"], "MP_PASS")
        self.assertEqual(pair["nmos"], "MN_PASS")
        self.assertEqual(pair["channel_net"], "CH")
        self.assertEqual(pair["gate_p"], "SN")
        self.assertEqual(pair["gate_n"], "S")
        self.assertTrue(pair["complementary"])

    def test_skips_common_gate_inv(self):
        circuit = Circuit()
        _add_tran(
            circuit,
            _make_tran("MP0", "Y", "A", "VDD", "pmos"),
        )
        _add_tran(
            circuit,
            _make_tran("MN0", "Y", "A", "VSS", "nmos"),
        )

        pairs = detect_tg_split_gate_pairs(circuit)
        self.assertEqual(pairs, [])

    def test_skips_unshared_channel(self):
        circuit = Circuit()
        _add_tran(
            circuit,
            _make_tran("MP1", "N1", "G1", "N2", "pmos"),
        )
        _add_tran(
            circuit,
            _make_tran("MN1", "N3", "G2", "N4", "nmos"),
        )

        pairs = detect_tg_split_gate_pairs(circuit)
        self.assertEqual(pairs, [])

    def test_gates_are_complementary_heuristics(self):
        self.assertTrue(gates_are_complementary("S", "SN"))
        self.assertTrue(gates_are_complementary("A", "B"))
        self.assertFalse(gates_are_complementary("A", "A"))


if __name__ == "__main__":
    unittest.main()
