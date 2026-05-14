# Sample report

This folder ships a small synthetic pentest report so the project
docs can show what the pipeline produces without depending on real
customer data.

## Files

| File | What it is |
|---|---|
| `sample_pentest_report.md` | Source markdown of a fake "NimbusBoard Workspace" pentest finding. All names, IPs, emails, hostnames and identifiers in this file are invented. |
| `sample_pentest_report.pdf` | The same content rendered to PDF via the project's normal Build pipeline. This is the **input** the redactor sees. |
| `sample_pentest_report.anonymized/sample_pentest_report.anonymized.pdf` | The **output** after running the anonymizer over the PDF. |

## This is a partially-anonymized example

The output PDF is intentionally **not a complete anonymization**:

- The substitution map shipped with the project covers most leaks
  in the source (`NimbusBoard`, `NimbusComm`, the CIDR ranges, the
  IDs `2026-Q1-NimbusBoard-Web` and `NIMB-FIND-2026-001`, the user
  names, the AWS UUID, etc.).
- A few leaks were left untouched on purpose, mainly the second
  finding's identifier `NIMB-FIND-2026-002`. They make it visible
  in the rendered output that the operator owns the final review:
  not every leak the detector flags reaches the substitution map,
  and the pipeline does not silently invent placeholders for
  things the operator did not approve.

So this artefact illustrates the **flow**, not a "perfect run".
For a complete redaction on a real document you would resolve
those residual rows through the GUI Review pane (or the CLI
`promote` step) before exporting.

## Why ship the intermediate `substitution_map.yml`?

We do not. The folder used to contain a few intermediate files
(`substitution_map.yml`, `applied_substitutions.json`, the
`.anon/` checkpoints, etc.) because they were the working state
of the developer's last run. They are part of the project's
[on-disk schema](../architecture.md#on-disk-schema), but they are
runtime artefacts: the pipeline regenerates them from the input
PDF + your operator decisions. Shipping them in the repo would
just freeze one snapshot of someone's local Review queue. The
folder now ships **only the rendered before/after PDFs** plus
this note.
