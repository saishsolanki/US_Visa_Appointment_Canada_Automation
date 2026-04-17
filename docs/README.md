# 📚 Documentation Hub

Your quick navigation center. Repository guides are at project root for backward compatibility.

---

## 🚀 Getting Started

**First time?**
1. Start with [Main README](../README.md) for overview and features
2. Choose your deployment: [Docker](../DOCKER_GUIDE.md) or [Native Linux/Windows](../README.md)
3. Configure settings: [CONFIGURATION.md](../CONFIGURATION.md)
4. Set up email: [GMAIL_SETUP_GUIDE](../GMAIL_SETUP_GUIDE.md)

---

## 📖 By Use Case

| Goal | Document |
|------|-----------|
| **Install & overview** | [README.md](../README.md) |
| **Configure all settings** | [CONFIGURATION.md](../CONFIGURATION.md) |
| **Use Docker** | [DOCKER_GUIDE.md](../DOCKER_GUIDE.md) |
| **Set up email alerts** | [GMAIL_SETUP_GUIDE.md](../GMAIL_SETUP_GUIDE.md) |
| **Update the software** | [UPDATE_GUIDE.md](../UPDATE_GUIDE.md) |
| **Fix an error quickly** | [QUICK_FIX.md](../QUICK_FIX.md) |
| **Troubleshoot issues** | [FAQ.md](../FAQ.md) |
| **Maximize appointment chances** | [STRATEGIC_OPTIMIZATION_GUIDE.md](../STRATEGIC_OPTIMIZATION_GUIDE.md) |
| **Understand performance gains** | [PERFORMANCE_OPTIMIZATIONS.md](../PERFORMANCE_OPTIMIZATIONS.md) |
| **Keep credentials safe** | [SECURITY.md](../SECURITY.md) |
| **Check what changed** | [CHANGELOG.md](../CHANGELOG.md) |

---

## 🔧 Advanced

- **Sensitive data handling**: [SANITIZATION_NOTE.md](../SANITIZATION_NOTE.md)
- **Ecosystem context**: [US_VISA_AUTOMATION_ECOSYSTEM_ANALYSIS.md](../US_VISA_AUTOMATION_ECOSYSTEM_ANALYSIS.md)

---

## 📝 Installation Scripts

All platforms now use the unified installer:

```bash
./install.sh              # Linux (auto-detects apt/dnf/pacman)
python install.py         # Cross-platform
install.bat              # Windows
```

Legacy distro scripts still work but delegate to `install.sh`:
- `install_ubuntu.sh`
- `install_debian.sh`
- `install_fedora.sh`
- `install_arch.sh`
- `install_kali.sh`

