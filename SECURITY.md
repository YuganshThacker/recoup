# Security Policy

## Scope

This repository is an experimental payment-recovery system. Do not use the public repository with real customer credentials or production payment data.

## Reporting

Please report security vulnerabilities privately through GitHub's security reporting mechanisms when available. Do not include live credentials, payment identifiers, or customer data in public issues.

## Design Principle

The system treats model output as untrusted input. Provider authentication, policy enforcement, idempotency, and execution controls remain outside the model.
