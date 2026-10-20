import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
path = ROOT/"models/reference/src/reference.py"
spec = importlib.util.spec_from_file_location("repressilator_reference_tests_module", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Reference = module.RepressilatorReference

class ReferenceTests(unittest.TestCase):
    def test_source_identity_and_initial_state(self):
        self.assertEqual(hashlib.sha256((ROOT/"evidence/BIOMD0000000012_url.xml").read_bytes()).hexdigest(),module.SOURCE_SHA256)
        r = Reference()
        r.setup()
        np.testing.assert_allclose([r._runner[s] for s in module.SPECIES],[0,20,0,0,0,0],rtol=0,atol=1e-12)
        self.assertEqual(r.outputs()["trajectory"].emitted_unit,"copies/cell")
        self.assertAlmostEqual(r._runner["a_tr"],29.97,places=10)
        self.assertAlmostEqual(r._runner["k_tl"],20*np.log(2)/2,places=10)
        self.assertAlmostEqual(r._runner["alpha"],29.97*20*10/(np.log(2)*40),places=8)

    def test_nonzero_checkpoint_continuation(self):
        uninterrupted = Reference()
        uninterrupted.setup()
        uninterrupted.advance_window(0,1000)
        checkpoint = json.loads(json.dumps(uninterrupted.snapshot()))
        uninterrupted.advance_window(1000,2000)
        restored = Reference()
        restored.restore(checkpoint)
        restored.advance_window(1000,2000)
        a = uninterrupted.get_outputs()["trajectory"].value["payload"]
        b = restored.get_outputs()["trajectory"].value["payload"]
        for sa,sb in zip(a["series"],b["series"]):
            va,vb=np.array(sa["points"]),np.array(sb["points"])
            np.testing.assert_allclose(va[:,0],vb[:,0],rtol=0,atol=1e-9)
            scale=np.ptp(va[:,1])
            self.assertLessEqual(np.max(abs(va[:,1]-vb[:,1]))/max(scale,1e-12),.001)
