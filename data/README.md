`index_constituents_sample.csv` is an unchanged copy of the supplied sample.

- Size: 3,888 bytes.
- Records: 54 (30 for BITA100, 24 for BITA-TECH50).
- Dates: 2026-01-02, 2026-04-01, 2026-07-01; 18 records on each date.
- No duplicate business keys within this file.
- SHA-256: `c88e8b46b3f30378c1db1bde694619d6507ece6ab8175cc4d202ea3ef3069aa0`.

The supplied JPM identifier `US478160104` has 11 characters and is preserved.
Do not save over this file through Excel. `.gitattributes` disables newline
conversion for it, and a regression test checks its byte fingerprint.
The upload response also includes a SHA-256 digest of the uploaded bytes.
