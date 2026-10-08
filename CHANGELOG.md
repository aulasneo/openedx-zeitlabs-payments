## Change Log

All enhancements and patches to zeitlabs_payments will be documented
in this file. It adheres to the structure of https://keepachangelog.com/ ,
but in reStructuredText instead of Markdown (for ease of incorporation into
Sphinx documentation and the PyPI description).

This project adheres to Semantic Versioning (https://semver.org/).

There should always be an "Unreleased" section for changes pending release.

## [Unreleased]

### Fixed

- Preserve fractional cart totals as Decimal in shared transaction parameters.
- Add exact provider precision and integer minor-unit conversion helpers.

### Changed

- Provider adapters must serialize the shared Decimal amount before signing or
  submitting it. Coordinate upgrades with external processors, especially
  PayFort, which must convert its amount to an integer after scaling.

[0.1.4] – 2025-11-30
**********************************************

### Fixed

- General bug fixes
- Enhanced invoice styling
- Some refactoring for better code maintainability

### Added

- SKU field set as unique in the database
- tox command for running makemigrations: `tox -e makemigrations`

[0.1.1] – 2025-09-15
**********************************************

### Fixed

- Set the correct license (AGPL v3)

[0.1.0] – 2025-09-11
**********************************************

### Added

- First release
