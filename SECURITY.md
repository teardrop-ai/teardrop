# Security Policy

## Supported Versions

Only the latest version of Teardrop receives security fixes.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a security issue, email: **[YOUR SECURITY CONTACT EMAIL]**

Include as much detail as possible:
- A description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept if available)
- Affected version(s)

You will receive an acknowledgement within **3 business days** and a resolution
timeline within **10 business days** of triage.

## Scope

The following are considered in-scope:

- Authentication and authorisation bypass (JWT, SIWE, client-credentials)
- x402 payment verification or settlement bypass
- SQL injection, XSS, CSRF, or SSRF in any endpoint
- Secrets leakage (API keys, private keys, credentials) via any channel
- Privilege escalation (user → admin, cross-tenant data access)

The following are considered out-of-scope:

- Rate limiting exhaustion (in-memory limiter is a known limitation)
- Denial-of-service via legitimate API usage
- Vulnerabilities in third-party dependencies (report directly to the vendor)
- Issues only reproducible on unsupported configurations

## Disclosure Policy

We follow responsible disclosure. Once a fix is available, we aim to publish a
security advisory within 90 days of the initial report.
