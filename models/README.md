# PTM 180 nm

The required local `ptm180.lib` is the unmodified PTM beta 180 nm bulk NMOS/PMOS
model. It is not distributed with this repository. The model used for the
recorded evaluations came from the SJTU model collection mirror:

<https://github.com/SJTU-YONGFU-RESEARCH-GRP/spice_model_collections/blob/5d0e6df825958f1f73e71c86e867d8ed0ddb1821/ptm/180nm_bulk.pm>

SHA256: `95e8b217ee0a2e38b0c2430a417ca7cd46ceaabe640bd071ab1e5a7156f8d507`

This provenance record is not a license grant. The model file does not include
an explicit redistribution license. Confirm permission with the upstream
rights holder before redistributing it; the project's code license, if one is
added later, does not cover this model.

## Local setup

1. Review the upstream terms and obtain any permission required for your use.
2. Obtain `180nm_bulk.pm` from the pinned source above.
3. Save it as `models\ptm180.lib` without modifying its contents.
4. From the repository root, verify the expected checksum:

```powershell
$expected = '95e8b217ee0a2e38b0c2430a417ca7cd46ceaabe640bd071ab1e5a7156f8d507'
$actual = (Get-FileHash -LiteralPath 'models\ptm180.lib' -Algorithm SHA256).Hash
if ($actual -ne $expected) {
    throw 'The local transistor model does not match the documented version.'
}
```

Git ignores `ptm180.lib`, including model copies in local exports. Do not
force-add it to commits.

## Simulator limitations

The model cards use level 49 (BSIM3) and contain HSPICE-specific parameters
that ngspice may warn about and ignore. Simulator warnings are returned in
each result and kept in its log. No fallback is selected silently.

These are predictive educational models, not foundry-qualified models.
The cards' stated lmin/lmax are 0.18 um; use of longer lengths for this
prototype is an extrapolation, not a validated process guarantee. The model has
no process corners; the supply and temperature conditions used by the agent do
not substitute for them.
