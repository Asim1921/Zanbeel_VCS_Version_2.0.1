# Release Notes — {{PROJECT_NAME}}

<!-- Template version: 1.0 | Standard: Keep a Changelog (keepachangelog.com) / SemVer 2.0 -->
<!-- STRUCTURE REFERENCE: Each release block MUST contain all subsections below. -->

---

## Document Information

| Field | Value |
|-------|-------|
| Document Title | Release Notes / Changelog |
| Project | {{PROJECT_NAME}} |
| Maintained | Per release |
| Format | Keep a Changelog + Semantic Versioning |

---

## Guiding Principles

- **Added** — new features introduced in this release.
- **Changed** — changes to existing functionality (non-breaking).
- **Deprecated** — features that will be removed in a future release.
- **Removed** — features removed in this release.
- **Fixed** — bug fixes.
- **Security** — vulnerability patches.
- **Breaking Changes** — changes that require consumer action to migrate.

Versions follow [Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`

---

## [Unreleased]

> Features merged but not yet tagged as a release.

### Added
- ...

### Changed
- ...

---

## [{{VERSION}}] — {{DATE}}

> One-line summary of this release (e.g. "Initial public release with core repository management features").

### Added

List every new feature introduced. Each bullet MUST cite the implementing module,
function, endpoint, or class:

- **Feature Name** — Description. Implemented by `handler_function()` at `METHOD /api/path`.
- **Feature Name** — Description. Implemented by `ClassName` in `module.py`.

### Changed

List non-breaking changes to existing behaviour:

- **Change description** — What changed and why. Affects `affected_function()`.

### Deprecated

Features that still work but are marked for removal:

- `old_function()` in `module.py` — will be removed in v{{NEXT_MAJOR}}. Use `new_function()` instead.

### Removed

Features removed in this release:

- `removed_endpoint` (`METHOD /old/path`) — removed after deprecation in v{{PREV_VERSION}}.

### Fixed

Bug fixes. Reference issue number where known:

- Fixed `NullPointerException` in `function_name()` when input is empty (#123).
- Fixed incorrect HTTP status code returned by `handler()` for 404 cases.

### Security

- Updated `dependency_name` from `X.Y.Z` to `A.B.C` to patch CVE-YYYY-NNNNN.
- Enforced HTTPS-only cookies for session tokens.

### Breaking Changes

> ⚠️ Consumers **must** take action before upgrading.

- **API change:** `METHOD /old/path` renamed to `METHOD /new/path`. Update all API clients.
- **Config change:** `OLD_ENV_VAR` replaced by `NEW_ENV_VAR`. Update `.env` files.

---

## [{{PREV_VERSION}}] — {{PREV_DATE}}

*(Add historical release blocks following the same format above)*

### Added
- ...

### Fixed
- ...

---

## Upgrade Guide

### Upgrading from v{{PREV_VERSION}} to v{{VERSION}}

1. **Pull latest code:**

   ```bash
   git pull origin main
   ```

2. **Update dependencies:**

   ```bash
   <install-command>
   ```

3. **Run database migrations (if applicable):**

   ```bash
   <migration-command>
   ```

4. **Update environment variables** — see Breaking Changes section above.

5. **Restart the service:**

   ```bash
   <restart-command>
   ```

---

## Known Limitations

List known issues or limitations that are NOT fixes in this release:

| ID | Description | Workaround | Target Release |
|----|-------------|-----------|---------------|
| KL-001 | — | — | v{{NEXT_MINOR}} |
| KL-002 | — | None | TBD |

---

## Roadmap

Features planned for upcoming releases:

| Version | Feature | Description |
|---------|---------|-------------|
| v{{NEXT_MINOR}} | — | — |
| v{{NEXT_MAJOR}} | — | — |

---

## Contributors

Thanks to all contributors in this release:

- @username — description of contribution

---

*End of Release Notes*
