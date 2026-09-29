# M2.17c — DMV frozen-sample singleton refresh

The persisted sample `59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f` was replayed without random sampling. Baseline objective `42791.986480127205` and estimate-vector digest `f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f` passed exactly. All 72 candidates were evaluated twice from empty design; the semantic profile digest `e85f2392fd95ac2a122c1f9ff4702a2246e6913e7db13a662c10e26de6fb95ab` matched exactly between runs. Realizations were {'PRESENT': 70, 'ABSENT_NATIVE': 2}.

The M2.16 maintenance model remains a separate production-style ANALYZE model; frozen replay timings were not used for calibration. Historical M2.15 comparison is descriptive only.
