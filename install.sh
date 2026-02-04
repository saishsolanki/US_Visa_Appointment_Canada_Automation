#!/bin/bash
# =============================================================================
# US Visa Appointment Checker - Ubuntu One-Command Installer
# 
# Usage: curl -fsSL https://raw.githubusercontent.com/saishsolanki/US_Visa_Appointment_Canada_Automation/main/install.sh | bash
# 
# Or after cloning: ./install.sh
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     🇺🇸 US Visa Appointment Checker - Installer              ║"
echo "║                                                              ║"
echo "║     Automated appointment monitoring for AIS portal          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if running on Linux
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    echo -e "${RED}❌ This installer is designed for Linux (Ubuntu/Debian).${NC}"
    echo "For Windows, use install.bat or run manually with Python."
    exit 1
fi

# Get the script directory (works for both curl pipe and direct run)
SCRIPT_DIR="$(pwd)"
if [[ -f "visa_appointment_checker.py" ]]; then
    echo -e "${GREEN}✓ Found existing installation in current directory${NC}"
else
    echo -e "${YELLOW}📥 Cloning repository...${NC}"
    git clone https://github.com/saishsolanki/US_Visa_Appointment_Canada_Automation.git
    cd US_Visa_Appointment_Canada_Automation
    SCRIPT_DIR="$(pwd)"
fi

echo ""
echo -e "${YELLOW}Choose installation method:${NC}"
echo "  1) Docker (Recommended - runs in container, auto-restarts)"
echo "  2) Native (Python + virtual environment + systemd service)"
echo ""
read -p "Enter choice [1/2]: " install_choice

case $install_choice in
    1)
        echo ""
        echo -e "${BLUE}🐳 Installing with Docker...${NC}"
        
        # Check if Docker is installed
        if ! command -v docker &> /dev/null; then
            echo -e "${YELLOW}Installing Docker...${NC}"
            
            # Install Docker prerequisites
            sudo apt-get update
            sudo apt-get install -y ca-certificates curl gnupg
            
            # Add Docker's GPG key
            sudo install -m 0755 -d /etc/apt/keyrings
            if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
                curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.asc
                sudo chmod a+r /etc/apt/keyrings/docker.asc
            fi
            
            # Add Docker repository
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            
            # Install Docker
            sudo apt-get update
            sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
            
            # Add user to docker group
            sudo usermod -aG docker $USER
            
            echo -e "${GREEN}✓ Docker installed${NC}"
            echo -e "${YELLOW}⚠ You may need to log out and back in for docker group to take effect${NC}"
        else
            echo -e "${GREEN}✓ Docker already installed ($(docker --version))${NC}"
        fi
        
        # Create config if needed
        if [[ ! -f "config.ini" ]]; then
            echo ""
            echo -e "${BLUE}📝 Let's configure your settings...${NC}"
            
            read -p "AIS Portal Email: " ais_email
            read -sp "AIS Portal Password: " ais_password
            echo
            read -p "Current Appointment Date (YYYY-MM-DD): " current_date
            read -p "Embassy Location (e.g., Ottawa - U.S. Embassy): " location
            read -p "Start Date for search (YYYY-MM-DD): " start_date
            read -p "End Date for search (YYYY-MM-DD): " end_date
            echo ""
            echo -e "${YELLOW}📧 Email Notifications (Gmail recommended)${NC}"
            read -p "Gmail address for sending: " gmail_user
            read -sp "Gmail App Password (16 chars): " gmail_pass
            echo
            read -p "Email to receive notifications [$gmail_user]: " notify_email
            notify_email="${notify_email:-$gmail_user}"
            
            # Create config from template
            cp config.ini.template config.ini
            
            # Replace values (using | as delimiter to handle special chars)
            sed -i "s|your_ais_email@example.com|$ais_email|g" config.ini
            sed -i "s|your_ais_password|$ais_password|g" config.ini
            sed -i "s|current_appointment_date = .*|current_appointment_date = $current_date|g" config.ini
            sed -i "s|location = .*|location = $location|g" config.ini
            sed -i "s|start_date = .*|start_date = $start_date|g" config.ini
            sed -i "s|end_date = .*|end_date = $end_date|g" config.ini
            sed -i "s|your_gmail@gmail.com|$gmail_user|g" config.ini
            sed -i "s|your_gmail_app_password|$gmail_pass|g" config.ini
            sed -i "s|your_notification_email@gmail.com|$notify_email|g" config.ini
            
            echo -e "${GREEN}✓ Configuration saved to config.ini${NC}"
        else
            echo -e "${GREEN}✓ Using existing config.ini${NC}"
        fi
        
        # Create logs and artifacts directories
        mkdir -p logs artifacts
        
        # Build and start Docker container
        echo ""
        echo -e "${BLUE}🔨 Building Docker image...${NC}"
        
        # Use sudo if not in docker group yet
        if groups | grep -q docker; then
            docker compose build
            docker compose up -d
        else
            sudo docker compose build
            sudo docker compose up -d
        fi
        
        echo ""
        echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
        echo -e "${GREEN}✅ Installation Complete!${NC}"
        echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
        echo ""
        echo -e "📍 ${YELLOW}View logs:${NC}         docker compose logs -f"
        echo -e "📍 ${YELLOW}Stop checker:${NC}      docker compose down"
        echo -e "📍 ${YELLOW}Restart:${NC}           docker compose restart"
        echo -e "📍 ${YELLOW}Check status:${NC}      docker compose ps"
        echo ""
        echo -e "📧 Progress reports will be emailed every 6 hours"
        echo -e "📂 Logs saved to: ${SCRIPT_DIR}/logs/"
        ;;
        
    2)
        echo ""
        echo -e "${BLUE}🐍 Installing Native (Python + systemd)...${NC}"
        
        # Install system dependencies
        echo -e "${YELLOW}Installing system dependencies...${NC}"
        sudo apt-get update
        sudo apt-get install -y python3 python3-pip python3-venv wget gnupg curl
        
        # Install Chrome if not present
        if ! command -v google-chrome &> /dev/null; then
            echo -e "${YELLOW}Installing Google Chrome...${NC}"
            wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
            echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
            sudo apt-get update
            sudo apt-get install -y google-chrome-stable
            echo -e "${GREEN}✓ Google Chrome installed${NC}"
        else
            echo -e "${GREEN}✓ Google Chrome already installed${NC}"
        fi
        
        # Create virtual environment
        echo -e "${YELLOW}Setting up Python environment...${NC}"
        python3 -m venv visa_env
        source visa_env/bin/activate
        pip install --upgrade pip
        pip install -r requirements.txt
        echo -e "${GREEN}✓ Python dependencies installed${NC}"
        
        # Create config if needed
        if [[ ! -f "config.ini" ]]; then
            echo ""
            echo -e "${BLUE}📝 Let's configure your settings...${NC}"
            
            read -p "AIS Portal Email: " ais_email
            read -sp "AIS Portal Password: " ais_password
            echo
            read -p "Current Appointment Date (YYYY-MM-DD): " current_date
            read -p "Embassy Location (e.g., Ottawa - U.S. Embassy): " location
            read -p "Start Date for search (YYYY-MM-DD): " start_date
            read -p "End Date for search (YYYY-MM-DD): " end_date
            echo ""
            echo -e "${YELLOW}📧 Email Notifications (Gmail recommended)${NC}"
            read -p "Gmail address for sending: " gmail_user
            read -sp "Gmail App Password (16 chars): " gmail_pass
            echo
            read -p "Email to receive notifications [$gmail_user]: " notify_email
            notify_email="${notify_email:-$gmail_user}"
            
            cp config.ini.template config.ini
            sed -i "s|your_ais_email@example.com|$ais_email|g" config.ini
            sed -i "s|your_ais_password|$ais_password|g" config.ini
            sed -i "s|current_appointment_date = .*|current_appointment_date = $current_date|g" config.ini
            sed -i "s|location = .*|location = $location|g" config.ini
            sed -i "s|start_date = .*|start_date = $start_date|g" config.ini
            sed -i "s|end_date = .*|end_date = $end_date|g" config.ini
            sed -i "s|your_gmail@gmail.com|$gmail_user|g" config.ini
            sed -i "s|your_gmail_app_password|$gmail_pass|g" config.ini
            sed -i "s|your_notification_email@gmail.com|$notify_email|g" config.ini
            
            echo -e "${GREEN}✓ Configuration saved${NC}"
        fi
        
        # Create directories
        mkdir -p logs artifacts
        
        # Create systemd service
        echo -e "${YELLOW}Creating systemd service...${NC}"
        
        SERVICE_FILE="/etc/systemd/system/visa-checker.service"
        sudo tee $SERVICE_FILE > /dev/null <<EOF
[Unit]
Description=US Visa Appointment Checker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/visa_env/bin/python $SCRIPT_DIR/visa_appointment_checker.py --frequency 5
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
        
        sudo systemctl daemon-reload
        sudo systemctl enable visa-checker.service
        sudo systemctl start visa-checker.service
        
        echo ""
        echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
        echo -e "${GREEN}✅ Installation Complete!${NC}"
        echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
        echo ""
        echo -e "📍 ${YELLOW}View logs:${NC}         sudo journalctl -u visa-checker -f"
        echo -e "📍 ${YELLOW}Stop checker:${NC}      sudo systemctl stop visa-checker"
        echo -e "📍 ${YELLOW}Restart:${NC}           sudo systemctl restart visa-checker"
        echo -e "📍 ${YELLOW}Check status:${NC}      sudo systemctl status visa-checker"
        echo ""
        echo -e "📧 Progress reports will be emailed every 6 hours"
        echo -e "📂 Logs saved to: ${SCRIPT_DIR}/logs/"
        ;;
        
    *)
        echo -e "${RED}Invalid choice. Exiting.${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${BLUE}📖 For more information, see:${NC}"
echo "   - DOCKER_GUIDE.md (for Docker deployment)"
echo "   - GMAIL_SETUP_GUIDE.md (for email notifications)"
echo "   - README.md (general usage)"
echo ""
