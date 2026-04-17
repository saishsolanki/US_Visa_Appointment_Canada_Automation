# Documentation Index

This repository keeps operational guides in markdown files at the project root for backward compatibility with existing links.

Use this page as the single entry point:

- Setup and onboarding: ../README.md
- Docker usage: ../DOCKER_GUIDE.md
- Security practices: ../SECURITY.md
- Gmail setup: ../GMAIL_SETUP_GUIDE.md
- Performance strategy: ../PERFORMANCE_OPTIMIZATIONS.md
- Strategic tuning: ../STRATEGIC_OPTIMIZATION_GUIDE.md
- Troubleshooting and quick fixes: ../FAQ.md and ../QUICK_FIX.md
- Changelog and updates: ../CHANGELOG.md and ../UPDATE_GUIDE.md

## Installer Consolidation

Legacy distro-specific scripts now delegate to the unified installer:

- ../install_ubuntu.sh
- ../install_debian.sh
- ../install_fedora.sh
- ../install_arch.sh
- ../install_kali.sh

Use ../install.sh for all Linux distributions. It auto-detects apt, dnf, or pacman.
