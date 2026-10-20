import importlib.util
from pathlib import Path
import sys
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("repressilator_comparison_core_tests",ROOT/"models/comparison/src/core.py")
core=importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

class ComparisonTests(unittest.TestCase):
    def test_physical_equations_and_rescaling(self):
        np.testing.assert_allclose(core.rhs(0,np.zeros(6)),[30,30,30,0,0,0],rtol=0,atol=1e-12)
        y=np.array([3.,5.,7.,40.,80.,120.])
        v=core.rhs(0,y)
        p=y[3:]/40
        m=core.KT*y[:3]/(core.GAMMA_P*40)
        alpha=29.97*20/(core.GAMMA_P*40)
        alpha0=.03*20/(core.GAMMA_P*40)
        expected_m=-m+alpha/(1+p[[2,0,1]]**2)+alpha0
        np.testing.assert_allclose(v[:3]*core.KT/(core.GAMMA_P*40*core.GAMMA_M),expected_m,rtol=1e-12)
        np.testing.assert_allclose(v[3:]/(40*core.GAMMA_M),.2*(m-p),rtol=1e-12)

    def test_negative_missing_and_bad_method_inputs(self):
        with self.assertRaises(ValueError):
            core.integrate("unsupported")
        with self.assertRaises(ValueError):
            core.evaluate([0.],[[0.]*6])
        with self.assertRaises(ValueError):
            core.metrics(np.arange(20),np.zeros((20,3)))

    def test_solver_robustness_and_computed_metrics(self):
        primary=core.integrate()
        tight=core.integrate("Radau")
        self.assertTrue(np.isfinite(primary).all())
        self.assertGreaterEqual(primary.min(),-1e-8)
        pm=core.metrics(core.TIME,primary[:,3:])
        tm=core.metrics(core.TIME,tight[:,3:])
        results=core.compare(core.TIME,tight,primary,tm,pm,True)
        self.assertTrue(all(x["passed"] for x in results),results)
        self.assertTrue(all(x["complete_cycles"]>=3 for x in pm))
        self.assertTrue(all(x["amplitude_drift_fraction"]<=.01 for x in pm))

    def test_port_policy_and_visual_agree_with_saved_values(self):
        # Load as a package so the component's relative core import is exercised.
        spec=importlib.util.spec_from_file_location("repressilator_comparison_package",ROOT/"models/comparison/src/__init__.py",
                                                  submodule_search_locations=[str(ROOT/"models/comparison/src")])
        package=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=package
        spec.loader.exec_module(package)
        from importlib import import_module
        cls=import_module(spec.name+".comparison").RepressilatorComparison
        from biosimulant import ExecutionPolicy
        m=cls()
        self.assertEqual(m.execution_policy,ExecutionPolicy.ONCE_AFTER_RUN)
        self.assertEqual(m.inputs()["reference"].schema,{"payload":"json"})
        self.assertEqual(m.outputs()["trajectories"].emitted_unit,"copies/cell")
        values=core.integrate()
        m.traces={"time_min":core.TIME.tolist(),"primary":values.tolist()}
        v=m.visualize()
        self.assertEqual(v["render"],"timeseries")
        self.assertEqual(v["data"]["x_unit"],"min")
        self.assertEqual(v["data"]["y_unit"],"copies/cell")
        for j,series in enumerate(v["data"]["series"]):
            np.testing.assert_allclose(np.array(series["points"])[:,1],values[:2001:4,j+3],rtol=0,atol=0)
