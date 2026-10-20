"""Independent physical six-ODE implementation and prespecified comparisons."""
import numpy as np
from scipy.integrate import solve_ivp
from scipy.signal import find_peaks

SPECIES = ["X", "Y", "Z", "PX", "PY", "PZ"]
PROTEINS = ["LacI", "TetR", "cI"]
INITIAL = np.array([0., 20., 0., 0., 0., 0.])
GAMMA_M = np.log(2.) / 2.
GAMMA_P = np.log(2.) / 10.
KT = 20. * GAMMA_M
TIME = np.arange(4001, dtype=float) * 0.5

def rhs(t, y):
    mrna, protein = y[:3], y[3:]
    repressors = protein[[2, 0, 1]]
    production = 0.03 + 29.97 / (1. + (repressors / 40.) ** 2)
    return np.concatenate((production - GAMMA_M * mrna, KT * mrna - GAMMA_P * protein))

def integrate(method="DOP853"):
    if method not in ("DOP853", "Radau"):
        raise ValueError("Unsupported verification method")
    tight = method == "Radau"
    result = solve_ivp(rhs, (0., 2000.), INITIAL.copy(), t_eval=TIME,
                       method=method, rtol=1e-11 if tight else 1e-9,
                       atol=1e-13 if tight else 1e-11,
                       max_step=0.25 if tight else 0.5)
    if not result.success or result.y.shape != (6, 4001):
        raise RuntimeError("Independent integration failed: " + result.message)
    return result.y.T

def refine(t, values, indexes):
    times, heights = [], []
    for i in indexes:
        if i == 0 or i == len(values)-1:
            continue
        a, b, c = values[i-1:i+2]
        denominator = a - 2*b + c
        delta = 0. if abs(denominator) < 1e-25 else 0.5*(a-c)/denominator
        delta = float(np.clip(delta, -1., 1.))
        times.append(float(t[i] + delta*(t[i+1]-t[i])))
        heights.append(float(b - 0.25*(a-c)*delta))
    return np.array(times), np.array(heights)

def metrics(time, proteins):
    t = np.asarray(time, dtype=float)
    p = np.asarray(proteins, dtype=float)
    mask = t >= 1000.
    t, p = t[mask], p[mask]
    answer = []
    for col in range(3):
        v = p[:, col]
        pk, _ = find_peaks(v)
        tr, _ = find_peaks(-v)
        pt, pv = refine(t, v, pk)
        tt, negtv = refine(t, -v, tr)
        tv = -negtv
        amps = []
        for j in range(len(pt)-1):
            inside = (tt > pt[j]) & (tt < pt[j+1])
            if np.any(inside):
                amps.append(float((pv[j]+pv[j+1])/2. - np.min(tv[inside])))
        if len(amps) < 3 or len(pt) < 4:
            raise ValueError("Fewer than three complete tail cycles for " + PROTEINS[col])
        amplitude = float(np.median(amps))
        if amplitude <= 0:
            raise ValueError("Nonpositive oscillation amplitude")
        answer.append({"protein": PROTEINS[col], "period_min": float(np.median(np.diff(pt))),
                       "amplitude_copies_per_cell": amplitude,
                       "amplitude_drift_fraction": float(np.ptp(amps)/amplitude),
                       "complete_cycles": len(amps), "peak_times_min": pt.tolist(),
                       "peak_values_copies_per_cell": pv.tolist()})
    period = answer[0]["period_min"]
    anchor = np.array(answer[0]["peak_times_min"])
    for m in answer:
        peaks = np.array(m["peak_times_min"])
        phases = []
        for a in anchor[1:-1]:
            delta = (peaks - a) / period
            phases.append(float(delta[np.argmin(abs(delta))] % 1.))
        # Circular mean avoids a phase close to 1 averaging to 0.5 with one near 0.
        angle = np.angle(np.mean(np.exp(2j*np.pi*np.array(phases))))
        m["phase_from_LacI_cycles"] = float((angle/(2*np.pi)) % 1.)
    return answer

def circular_error(a, b):
    return float(abs((a-b+0.5) % 1. - 0.5))

def compare(time, candidate, reference, candidate_metrics, ref_metrics, numerical=False):
    mask = np.asarray(time) >= 1000.
    rows = []
    for j, (m, r) in enumerate(zip(candidate_metrics, ref_metrics)):
        scale = r["amplitude_copies_per_cell"]
        difference = np.asarray(candidate)[mask, j+3] - np.asarray(reference)[mask, j+3]
        peak_error = float(np.max(np.abs(difference))/scale)
        nrmse = float(np.sqrt(np.mean(difference**2))/scale)
        pe = abs(m["period_min"]/r["period_min"]-1.)
        ae = abs(m["amplitude_copies_per_cell"]/scale-1.)
        phase = circular_error(m["phase_from_LacI_cycles"], r["phase_from_LacI_cycles"])
        passed = (peak_error <= .001 and pe <= .001 and ae <= .001) if numerical else (nrmse <= .01 and pe <= .01 and ae <= .01 and phase <= .01)
        rows.append({"protein": PROTEINS[j], "period_relative_error": float(pe),
                     "amplitude_relative_error": float(ae), "phase_error_cycles": phase,
                     "trajectory_max_normalized_error": peak_error,
                     "trajectory_nrmse": nrmse, "passed": bool(passed)})
    return rows

def evaluate(reference_time, reference_states):
    time = np.asarray(reference_time, dtype=float)
    ref = np.asarray(reference_states, dtype=float)
    if time.shape != TIME.shape or not np.allclose(time, TIME, rtol=0, atol=1e-9):
        raise ValueError("Native reference must retain the frozen 0.5-minute grid")
    if ref.shape != (4001, 6) or not np.allclose(ref[0], INITIAL, rtol=0, atol=1e-10):
        raise ValueError("Native species order or initial state differs from the source")
    primary, tight = integrate(), integrate("Radau")
    summaries = {name: metrics(time, a[:, 3:]) for name, a in
                 [("primary", primary), ("tighter", tight), ("reference", ref)]}
    numerical = compare(time, tight, primary, summaries["tighter"], summaries["primary"], True)
    parity = compare(time, primary, ref, summaries["primary"], summaries["reference"])
    finite = all(np.isfinite(a).all() for a in [primary, tight, ref])
    nonnegative = all(np.min(a) >= -1e-8 for a in [primary, tight, ref])
    sustained = all(m["complete_cycles"] >= 3 and m["amplitude_drift_fraction"] <= .01
                    for m in summaries["primary"])
    fig = []
    # Figure readings and uncertainty were fixed in SOURCE_REVIEW before evaluation.
    for m in summaries["primary"]:
        period, amp = m["period_min"], m["amplitude_copies_per_cell"]
        period_error = abs(period/140.-1.)
        amplitude_error = abs(amp/2300.-1.)
        period_bounds = [float(abs(period/x-1.)) for x in [130., 150.]]
        amp_bounds = [float(abs(amp/x-1.)) for x in [2100., 2500.]]
        def bounded_status(bounds):
            return "within" if max(bounds) <= .10 else ("outside" if min(bounds) > .10 else "indeterminate")
        phase = m["phase_from_LacI_cycles"]
        spacing_error = min(circular_error(phase, x) for x in [0., 1/3, 2/3])
        fig.append({"protein":m["protein"], "period_point_relative_error":float(period_error),
                    "amplitude_point_relative_error":float(amplitude_error),
                    "period_uncertainty_status":bounded_status(period_bounds),
                    "amplitude_uncertainty_status":bounded_status(amp_bounds),
                    "phase_spacing_error_cycles":float(spacing_error)})
    checks = {"finite":bool(finite), "nonnegative":bool(nonnegative), "sustained":bool(sustained),
              "numerical":all(x["passed"] for x in numerical),
              "reference":all(x["passed"] for x in parity)}
    report = {"checks": checks, "metrics":summaries, "numerical_comparison":numerical,
              "reference_comparison":parity, "figure_comparison":fig,
              "classification":"partial reproduction" if all(checks.values()) else "reproduction not established",
              "classification_reason":"Figure initial state and raw trajectories are unavailable. Coarse figure agreement cannot establish exact waveform reproduction.",
              "units":{"time":"min","states":"copies/cell","period":"min","amplitude":"copies/cell","phase":"cycle","relative_errors":"1"},
              "settings":{"duration_min":2000.,"sampling_min":.5,"evaluation_window_min":[1000.,2000.],
                          "primary":{"method":"DOP853","rtol":1e-9,"atol":1e-11,"max_step_min":.5},
                          "tighter":{"method":"Radau","rtol":1e-11,"atol":1e-13,"max_step_min":.25},
                          "initial_state":INITIAL.tolist(),"species":SPECIES,"time_origin_shift_min":0.}}
    traces = {"time_min":time.tolist(),"species":SPECIES,"primary":primary.tolist(),
              "tighter":tight.tolist(),"reference":ref.tolist(),"quantity_unit":"copies/cell"}
    return traces, report
