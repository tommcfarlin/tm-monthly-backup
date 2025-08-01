# Development Workflow

## Branch Strategy

**NEVER work directly on main branch.**

### Branch Types
- `feature/` - New functionality
- `fix/` - Bug fixes
- `chore/` - Maintenance tasks (dependencies, docs)
- `update/` - Updates to existing features

### Workflow
1. Create feature branch from main: `git checkout -b feature/your-feature-name`
2. Make changes and commit with descriptive messages
3. Push branch and create Pull Request
4. Merge to main after review
5. Delete feature branch

## Commit Message Conventions

Format: `type: description`

Types:
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `chore:` - Maintenance tasks
- `test:` - Adding/updating tests
- `refactor:` - Code refactoring

Examples:
- `feat: add HEIC to JPEG conversion with EXIF preservation`
- `fix: handle duplicate timestamps correctly`
- `docs: update CLI usage examples`

## Semantic Versioning

Format: MAJOR.MINOR.PATCH

- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

## Documentation Requirements

All changes must include:
- Updated README.md if user-facing
- CHANGELOG.md entry
- Code comments for complex logic
- Test coverage for new features

## Code Quality Standards

- All Python code must follow PEP 8
- Functions must have docstrings
- Error handling required for all file operations
- Comprehensive logging for debugging

## Release Process

1. Update CHANGELOG.md with new version
2. Tag release: `git tag v1.0.0`
3. Push tags: `git push origin --tags`
4. Create GitHub release with changelog notes