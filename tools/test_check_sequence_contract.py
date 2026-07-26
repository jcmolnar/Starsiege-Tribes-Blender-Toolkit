import importlib.util
import json
import os
import struct
import tempfile
import unittest


HERE = os.path.dirname(__file__)
SPEC = importlib.util.spec_from_file_location(
    "seqchk", os.path.join(HERE, "check_sequence_contract.py"))
SEQCHK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEQCHK)


def write_glb(path, nodes):
    payload = json.dumps({"asset": {"version": "2.0"}, "nodes": nodes},
                         separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload)
    with open(path, "wb") as stream:
        stream.write(struct.pack("<4sII", b"glTF", 2, total))
        stream.write(struct.pack("<I4s", len(payload), b"JSON"))
        stream.write(payload)


class DetailCoverageTests(unittest.TestCase):
    def sizes(self, nodes):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "shape.glb")
            write_glb(path, nodes)
            return SEQCHK.glb_effective_detail_sizes(path)

    def test_no_lod_names_gets_always_visible_fallback(self):
        self.assertEqual(self.sizes([{"name": "root"}]), [1.0])

    def test_one_lod_is_normalized_to_one(self):
        self.assertEqual(self.sizes([{"name": "root 25"}]), [1.0])

    def test_mount_suffix_under_detail_is_not_a_second_lod(self):
        nodes = [{"name": "root 25", "children": [1]},
                 {"name": "dummy eye36"}]
        self.assertEqual(self.sizes(nodes), [1.0])

    def test_multiple_lods_retain_their_real_floor(self):
        nodes = [{"name": "root 25"}, {"name": "root 5"}]
        self.assertEqual(self.sizes(nodes), [25.0, 5.0])

    def test_utility_only_shape_has_no_render_detail(self):
        self.assertEqual(self.sizes([{"name": "collision0"}]), [])

    def test_regression_decision_can_fail(self):
        # The original indoorgun regression: source had a size-1 catch-all,
        # converted output's lowest selectable detail remained size 25.
        self.assertFalse(SEQCHK.detail_coverage_ok([25.0, 5.0, 1.0], [25.0]))

    def test_one_lod_runtime_fix_passes(self):
        self.assertTrue(SEQCHK.detail_coverage_ok([25.0, 5.0, 1.0], [1.0]))


if __name__ == "__main__":
    unittest.main()
