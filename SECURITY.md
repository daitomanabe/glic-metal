# Security policy

## Supported version

Security fixes are applied to the latest commit on `main`. No older release
line is currently maintained.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use the repository's
private security-advisory reporting channel. If private reporting is not yet
enabled, contact the maintainer through the address on the repository owner's
public profile and include `GLIC Metal security` in the subject.

Include the affected revision, operating system, reproduction steps, expected
impact, and whether camera access or crafted image/GLIC input is required. Do
not include real camera footage or other personal data unless it is essential
and explicitly requested.

This project processes untrusted binary and image data. Until a report is
resolved, avoid opening unknown `.glic` files and run the command-line tools
with normal user privileges.
