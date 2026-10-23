"""Profile one Home replay (the dishwasher scorer's replay: SUBS=prod HOUSE=prod SLICE=0) under
cProfile and print the hottest functions by own and cumulative time.
Usage: python3.14 tests/profile_replay.py [DIAL=V ...]   (writes data/profile.pstats)"""
import cProfile, pstats, sys, os, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "dishwasher"))
import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod", "SLICE=0", *sys.argv[1:]])
out = os.path.join(os.path.dirname(__file__), "..", "data", "profile.pstats")
cProfile.run('B._run(W.FOLDER, "home")', out)
s = io.StringIO()
st = pstats.Stats(out, stream=s)
st.sort_stats("tottime").print_stats(25)
st.sort_stats("cumulative").print_stats(25)
print(s.getvalue())
