# Contributing to gluetun-monitor

Thank you for your interest in contributing! This document provides guidelines and information for contributors.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Copy example configs:
   ```bash
   cp docker-compose.yml.example docker-compose.yml
   cp sites.conf.example sites.conf
   ```
4. Make your changes
5. Test your changes
6. Submit a pull request

## Development Setup

### Prerequisites

- Docker and Docker Compose
- Bash 4.0+
- [shellcheck](https://github.com/koalaman/shellcheck) for linting
- [bats](https://github.com/bats-core/bats-core) for testing (optional, runs in CI)

### Running Locally

```bash
# Build and run
docker compose build
docker compose up -d

# View logs
docker logs -f gluetun-monitor
```

### Running Tests

```bash
# Run shellcheck
shellcheck gluetun-monitor.sh

# Run bats tests (requires bats installed)
bats tests/
```

## Code Style

### Shell Script Guidelines

- Use `#!/bin/bash` shebang
- Enable strict mode: `set -euo pipefail`
- Declare variables with `local` in functions
- Separate declaration and assignment for command substitution:
  ```bash
  # Good
  local result
  result=$(some_command)

  # Bad (masks return value)
  local result=$(some_command)
  ```
- Quote all variables: `"$variable"` not `$variable`
- Use `[[` instead of `[` for conditionals
- Run shellcheck before committing

### Commit Messages

- Use present tense ("Add feature" not "Added feature")
- Use imperative mood ("Move cursor to..." not "Moves cursor to...")
- Keep the first line under 72 characters
- Reference issues when applicable: "Fix #123"

## Pull Request Process

1. Ensure shellcheck passes with no errors
2. Update documentation if you're changing behavior
3. Add tests for new functionality
4. Update CHANGELOG.md under "Unreleased"
5. Request review from maintainers

## Testing Guidelines

### What to Test

- New functions should have unit tests
- Bug fixes should include a regression test
- Integration tests for Docker-related changes

### Test Structure

Tests use [bats](https://github.com/bats-core/bats-core):

```bash
@test "description of what is being tested" {
    result=$(function_to_test)
    [ "$result" = "expected" ]
}
```

## Reporting Issues

- Use the issue templates provided
- Include relevant logs (sanitize sensitive info)
- Specify your environment (Docker version, OS, etc.)
- Check existing issues before creating a new one

## Feature Requests

- Open an issue using the feature request template
- Describe the problem you're trying to solve
- Propose a solution if you have one
- Be open to discussion and alternatives

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
