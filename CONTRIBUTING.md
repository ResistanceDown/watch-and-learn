# Contributing

Keep the three jobs distinct: gather evidence, evaluate it, and build within the user's authorization. Fix demonstrated failures with the smallest useful change. Keep dependencies optional where the requested task does not need them.

Run `python3 -m unittest discover -s tests -v` from the repository root. For reader changes, exercise a local synthetic recording and inspect `report.json`, `report.md`, and relevant images. Add a regression check for changed behavior; use standard-library tests and mock network calls. For skill changes, also run the scenarios in [docs/testing.md](docs/testing.md).

Keep root and compatibility manifest identity/presentation in sync. Bump the public semantic version for a release. Add actual repository or policy URLs only once they exist; do not insert placeholder links to satisfy a heuristic score.

Do not include local installation history, private deployments, raw video reports, cookies, credentials, or personal paths in contributions. Report a bug with tool versions, a minimal synthetic reproduction, and redacted errors. Preserve upstream attribution and license notices.
