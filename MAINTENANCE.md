# 🔧 Repository Maintenance Guide

This guide helps maintainers keep the repository organized and discoverable.

---

## GitHub "About" Section Configuration

To make the repository discoverable and help new users, configure GitHub's repository settings (Settings → General → About):

**Recommended "About" text**:
```
Automated US Visa appointment rescheduler for Canadian embassies. Monitors AIS portal 
continuously with 30-50% performance optimization. Docker & native installation.
→ Start with docs/README.md for navigation
```

**Homepage**: `https://github.com/saishsolanki/US_Visa_Appointment_Canada_Automation`

**Topics** (for searchability):
- `visa-automation`
- `usa-visa`
- `appointment-scheduling`
- `python`
- `selenium`
- `web-scraping`
- `canadian-visa`

---

## Documentation Hub Location

All users should start at **[docs/README.md](docs/README.md)** for navigation by use case.

The main README.md is reserved for:
- ✅ Feature overview
- ✅ Quick installation steps
- ✅ Basic configuration
- ✅ Release information
- ❌ NOT exhaustive setup details (direct to docs/README.md instead)

---

## Changelog Management

### Current Size Tracking

The CHANGELOG.md is currently **~70 lines** (well under 500-line limit).

**When to archive** (500+ lines):
1. Create `CHANGELOG_ARCHIVE.md` 
2. Move entries from v1.5.0 and earlier to archive
3. Keep v2.0.0+ in main CHANGELOG.md
4. Add note at top of CHANGELOG.md pointing to archive

**Archive command** (when needed):
```bash
# Extract entries up to v1.5.0 into CHANGELOG_ARCHIVE.md
# Keep v2.0.0+ in CHANGELOG.md
```

### Changelog Format

Follow [Keep a Changelog](https://keepachangelog.com/) conventions:
- `[Unreleased]` section at top for next release
- Sections: `Added`, `Changed`, `Fixed`, `Removed`, `Deprecated`
- Version tags: `[v2.0.0] - 2026-02-13`

---

## Documentation Structure

```
📁 Root
├── README.md                                  # Main entry point (overview + quick start)
├── CONFIGURATION.md                           # Config reference tables
├── docs/
│   ├── README.md                             # Navigation hub (start here!)
│   └── advanced/
│       └── README.md                         # Advanced topics & research
├── DOCKER_GUIDE.md, SECURITY.md, etc.       # Specific task guides
└── (other root .md files for quick access)
```

---

## Link Maintenance Checklist

When adding new docs or reorganizing:

- [ ] Update `docs/README.md` navigation table
- [ ] Add cross-references in related guides (e.g., UPDATE_GUIDE → QUICK_FIX)
- [ ] Verify links in README.md Documentation Resources section
- [ ] Check if any docs > 150 lines could be split
- [ ] Update GitHub "About" description if scope changes
- [ ] Validate all `.md` files are readable (no broken internal links)

---

## Discoverability Strategies

1. **docs/README.md** as single entry point — users should land here first
2. **CONFIGURATION.md** linked from README's config section
3. **Cross-references** between related guides (e.g., FAQ ↔ QUICK_FIX)
4. **GitHub Topics** on repo settings for search
5. **GitHub "About"** with clear, concise description

---

## File Size Guidelines

| Type | Target | Current |
|------|--------|---------|
| README.md | <400 lines | ~300 ✓ |
| CHANGELOG.md | <500 lines | ~70 ✓ |
| Task guides | <150 lines | All ✓ |
| docs/README.md | <100 lines | ~60 ✓ |

When files exceed targets, consider:
- Breaking into smaller docs
- Moving advanced content to `docs/advanced/`
- Archiving old changelog entries
