# Known limitations

**Labels arrive at least 7 days late.** Performance monitoring, which is what caught the new failure mode,
can only judge a day once its 7-day window has passed. Monitoring here works on whole months. A
production version should run drift checks daily on unlabelled data (the captured predictions) and
performance checks on a rolling window of matured labels.

**PSI stops being informative after the first big change.** Once the inputs have drifted, every later month
alerts on the same features until a retrained model resets the reference. In the replay, the fleet shift
and the new failure mode were "detected" in the month they started, but feature drift was already
alerting then. The measured performance drop is what showed the new failure mode was something new. A
better design would compare each month with the *previous* month as well as with the training reference,
and attribute alerts to new features.

**A new failure mode takes two months to learn.** The first retrain after seal leaks appeared was blocked:
PR-AUC 0.42 and recall 0.26 on compressors. That was correct, but production stayed weak for a month. Ways
to shorten this:
- label-efficient approaches (weighting recent failures)
- a separate model for the new mode
- a human-in-the-loop rule while data accumulates

**The gates use one holdout month.** About 200 positives make the comparison reasonably stable when both
models see the same month, but a single month can still favour one model by chance. Bootstrapped
confidence intervals on the PR-AUC difference would make `no_regression` and `improvement` statistical
rather than fixed tolerances.

**One threshold for all equipment types.** Precision and recall differ by fleet. Per-type thresholds, or a
cost-based threshold using real repair costs, would serve maintenance crews better.

**The scheduler runs one model.** The platform manages a single registered model. Several models would need
per-model settings and gates, and probably Step Functions instead of a single worker queue.

**Synthetic data.** The degradation physics is simplified, and drift happens exactly at month boundaries. Real
drift is gradual and messier. The lifecycle numbers show the platform's mechanisms work; they are not
a forecast for any real fleet.

**Not deployed.** The AWS path is exercised by unit tests (botocore Stubber for SageMaker and CloudWatch) and
`terraform validate`, not by a live account. The SageMaker container contract (`serve`, `/ping`,
`/invocations`, port 8080) is checked in CI by running the image.
