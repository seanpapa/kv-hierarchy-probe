# Releasing

A release is a tag on `main` whose tree passed every check below. Nothing is published to a package
index; users install from a clone of the tag.

## Version

1. Set the package version in `pyproject.toml` (PEP 440, `MAJOR.MINOR.PATCH`; see
   [VERSIONING](VERSIONING.md)). Data contracts keep their own versions; list the ones this release
   writes and reads.
2. Move the `unreleased` entry in [CHANGELOG](../CHANGELOG.md) under the version and date. Say what
   changed for a user and what is still limited.

## Checks — all must pass on the tree being tagged

| Check | Command or method |
|---|---|
| Tests, schema checks included, no unexpected skips | `python3 -m pip install -e '.[test]'` then `python3 -m unittest discover -s tests` |
| First visit from a fresh environment | New virtual environment, a clean clone, then follow only `README.md` through `first-visit`, `geometry` and one `report` |
| Bundled verdicts | `first-visit` reports every expected demo verdict matched |
| Links | Every relative link resolves; every external link opens and upstream code links are pinned to a commit or tag |
| Published content | No hostname, username, internal address, private path, e-mail address, token, private-repository link or session URL in text **or** in image metadata; no raw evidence bundles, archives, caches or build products in the tree |
| Licences and notices | `LICENSE`, `NOTICE` and [assets/README](../assets/README.md) match the files shipped |

A failed check is a no-go: fix it and run every check again on the new tree.

## Tag and publish

1. Tag the checked commit: `git tag -a v<version> -m "v<version>"`, then push the tag.
2. Create the GitHub release from the tag, with the CHANGELOG entry as its notes.
3. From a machine that was not used to build it, clone the tag anonymously and repeat the first-visit
   check.

## After a release

A published tag is never moved or rewritten. A defect in a release is fixed on `main` and shipped as
a new version; the CHANGELOG entry says which version it fixes. A release that exposed sensitive data
is handled through [SECURITY.md](../SECURITY.md) first.
