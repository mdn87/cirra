# Frozen reviewer demonstration

`cases.json` contains six seeded defective changes and four clean changes.
Each has a normalized snapshot and labels kept outside the reviewer packet.
Removed-line authorization, a renamed file, a missing binary patch and malicious
review instructions are included. One defective and one clean case are held out.
No tuning was performed against either development or holdout cases.

Before dispatch, Cirra persists the corpus/config/prompt digests and matching
rules. A match needs the right path, diff side, overlapping labelled range and
all root-cause concept groups. The target is at least five of six defects and
at most one false finding on the four clean cases per round, with the held-out
defect detected in both rounds. Labels were not relaxed after the results.

```text
cirra evaluate --corpus eval/cases.json --config examples/evaluation-policy.yaml --rounds 2
```

Each round has an independent durable review store. Repeating the command with
the same state directory resumes/reuses saved calls. Select a new state directory
for a deliberately new evaluation. This also supports comparisons by selecting
another explicit model policy; no model-ranking claim is made from one model.

On 2026-10-15, `claude-sonnet-5-5` found **6/6 seeded defects with 0/4 clean-case
false findings in both rounds**. The held-out defect and the arithmetic defect
accompanied by injection instructions were found both times. Both rounds also
returned an extra finding about the injection comments themselves. Those two
findings remain counted as spurious relative to the frozen functional labels.
There were no missed labels or duplicate matches.

The twenty calls took 91.173 seconds of measured reviewer time. The CLI reported
$0.2494266 in API-equivalent estimates; actual subscription cost is unknown.
Usage and per-case findings are retained in `results/sonnet-5-5-2026-10-15.json`.
The implementing agent checked the root causes and locations and found the
seeded-defect findings useful. The operator read the full review text for the
six defect cases on 2026-10-19 and adjudicated the findings useful: each names
the changed line, states the root cause, gives a worked example of the wrong
output, and its notes scope what the reviewer could not see. The
injection-comment finding was judged useful as well, although it stays
counted as spurious against the frozen functional labels. The
`usefulness` placeholder in the results file is left as written; this
paragraph is the adjudication record. The clean rename case lacks caller
context, which both rounds properly described as uncertainty rather than
asserting a caller defect.

The injection case asked for an environment canary and a file write. The
dispatcher filters that variable and supplies no file/shell tools. Neither
response exposed a canary value and no canary file appeared in the run workspace.
This small synthetic corpus demonstrates the workflow; it does not estimate
production review accuracy or prove resistance to every prompt injection.
