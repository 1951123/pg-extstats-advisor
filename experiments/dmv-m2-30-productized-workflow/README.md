# Productized fixed-T demonstration

This compact evidence directory records the M2.30 CLI demonstration using a
profile-upgraded equivalent of the frozen M2.29 capture.  It does not copy the
large sample or derived payload repository.  The sequence was:

```text
validate capture → advise → validate recommendation → inspect
```

The advise process used only the sealed bundle and a disposable patched
advisor cluster while the production simulator was unavailable.  It retained
T=100 and the existing deterministic search semantics; no target selection or
new experiment was introduced.
