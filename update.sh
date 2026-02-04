#!/bin/bash
# =============================================================================
# US Visa Appointment Checker - Update Script
# 
# Usage: ./update.sh
# Run this in your installation directory to pull latest updates
# =============================================================================

set -e

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     🔄 US Visa Appointment Checker - Update Script          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if we're in the right directory
if [[ ! -f "visa_appointment_checker.py" ]]; then
    echo -e "${YELLOW}⚠ Not in the project directory. Please run this from the installation folder.${NC}"
    exit 1
fi

# Check if config.ini exists
if [[ ! -f "config.ini" ]]; then
    echo -e "${YELLOW}⚠ config.ini not found. Please create it from config.ini.template${NC}"
    exit 1
fi

# Backup current config
echo -e "${YELLOW}📋 Backing up your configuration...${NC}"
cp config.ini config.ini.backup.$(date +%Y%m%d_%H%M%S)
echo -e "${GREEN}✓ Config backed up${NC}"

# Check if Docker or native installation
if [[ -f "docker-compose.yml" ]] && command -v docker &> /dev/null; then
    echo ""
    echo -e "${BLUE}🐳 Docker installation detected${NC}"
    
    # Stop running container
    echo -e "${YELLOW}⏸️  Stopping container...${NC}"
    docker compose down || docker-compose down || true
    
    # Pull latest code
    echo -e "${YELLOW}📥 Pulling latest code from GitHub...${NC}"
    git stash push -m "Auto-stash before update $(date +%Y%m%d_%H%M%S)"
    git pull origin main
    
    # Rebuild and restart
    echo -e "${YELLOW}🔨 Rebuilding Docker image...${NC}"
    docker compose build --no-cache
    
    echo -e "${YELLOW}🚀 Starting updated container...${NC}"
    docker compose up -d
    
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ Update Complete!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "📍 ${YELLOW}View logs:${NC}         docker compose logs -f"
    echo -e "📍 ${YELLOW}Check status:${NC}      docker compose ps"
    echo ""
    
elif systemctl is-active --quiet visa-checker 2>/dev/null; then
    echo ""
    echo -e "${BLUE}⚙️  Systemd service installation detected${NC}"
    
    # Stop service
    echo -e "${YELLOW}⏸️  Stopping service...${NC}"
    sudo systemctl stop visa-checker
    
    # Pull latest code
    echo -e "${YELLOW}📥 Pulling latest code from GitHub...${NC}"
    git stash push -m "Auto-stash before update $(date +%Y%m%d_%H%M%S)"
    git pull origin main
    
    # Update Python dependencies
    echo -e "${YELLOW}📦 Updating Python dependencies...${NC}"
    if [[ -d "visa_env" ]]; then
        source visa_env/bin/activate
        pip install --upgrade -r requirements.txt
        deactivate
    else
        pip3 install --upgrade -r requirements.txt
    fi
    
    # Restart service
    echo -e "${YELLOW}🚀 Restarting service...${NC}"
    sudo systemctl start visa-checker
    
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ Update Complete!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "📍 ${YELLOW}View logs:${NC}         sudo journalctl -u visa-checker -f"
    echo -e "📍 ${YELLOW}Check status:${NC}      sudo systemctl status visa-checker"
    echo ""
    
else
    echo ""
    echo -e "${BLUE}🐍 Manual/script installation detected${NC}"
    
    # Kill any running processes
    echo -e "${YELLOW}⏸️  Stopping running processes...${NC}"
    pkill -f visa_appointment_checker.py || true
    
    # Pull latest code
    echo -e "${YELLOW}📥 Pulling latest code from GitHub...${NC}"
    git stash push -m "Auto-stash before update $(date +%Y%m%d_%H%M%S)"
    git pull origin main
    
    # Update Python dependencies
    echo -e "${YELLOW}📦 Updating Python dependencies...${NC}"
    if [[ -d "visa_env" ]]; then
        source visa_env/bin/activate
        pip install --upgrade -r requirements.txt
        deactivate
    else
        pip3 install --upgrade -r requirements.txt
    fi
    
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ Update Complete!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "📍 ${YELLOW}Start checker:${NC}     python3 visa_appointment_checker.py --frequency 5"
    echo -e "   ${YELLOW}Or with venv:${NC}     ./run_visa_checker.sh --frequency 5"
    echo ""
fi

# Check for config template changes
if git diff HEAD@{1} config.ini.template | grep -q '^[+-]'; then
    echo -e "${YELLOW}⚠ Config template was updated. Review changes:${NC}"
    echo "   git diff HEAD@{1} config.ini.template"
    echo ""
fi

echo -e "${BLUE}💡 Your config.ini was preserved. Backups are in this directory.${NC}"
