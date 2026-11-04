# Security policy

## Report a vulnerability

Use GitHub's private
[Report a vulnerability](https://github.com/batchstream/weir-charts/security/advisories/new)
form. Include the affected chart version or commit, reproduction steps, impact,
and relevant deployment settings with credentials and private data removed.
Please keep exploit details out of public issues while a report is being
investigated.

## Versions

Check whether an issue is still present in the chart on `main`. Older chart
versions do not have a guaranteed security backport schedule. Report suspected
vulnerabilities even if you cannot reproduce them on `main`.

## Deployment guidance

Application traffic uses plaintext HTTP/2 inside a trusted, isolated network. See
[Network](docs/operations.md#network).
