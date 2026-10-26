# Imports car data from a directory of uploads into VictoriaMetrics, at the samples'
# original timestamps: Car Scanner recordings (.brc) and WiCAN Pro logger batches
# (.jsonl, .jsonl.gz). Each file is imported once, keyed by sha256 in the state file, so a
# re-upload adds nothing. Malformed files move to failed/; a file whose oldest sample is
# past VictoriaMetrics' retention stays where it is, recorded as too_old. The files stay
# in the directory as the long-term archive. Standard library only, shipped as one zipapp
# built by build.py.
# Every series carries a `car` label and a `trip` label. A .brc file is one trip, named by
# its stem. WiCAN rows are split into trips by gaps of more than 10 min, each named by its
# first row in local time, the shape of Car Scanner's stems; a batch continues its car's
# last trip when it starts within 10 min of it.
