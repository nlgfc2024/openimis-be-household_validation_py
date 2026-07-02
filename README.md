# openIMIS Backend household_validation Reference Module

`openimis-be-household_validation` is an openIMIS backend module scaffold for household validation features.

## Installation

For local development, place this repository next to `openimis-be_py`, then register it in `openimis-be_py/openimis.json`:

```json
{
  "name": "household_validation",
  "pip": "-e /home/yutaka/MSR_2026/June_2026/coremisalpha/openimis-be-household_validation_py"
}
```

Install or refresh backend module requirements from `openimis-be_py` as usual.

## Module Contents

The initial scaffold provides:

- Django app package: `household_validation`
- Required openIMIS URL configuration: `household_validation/urls.py`
- Python package metadata: `setup.py`
- License and manifest files

Feature models, GraphQL schema, services, permissions, and migrations can be added incrementally as the household validation requirements are finalized.
