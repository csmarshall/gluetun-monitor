#!/bin/bash
# Test helper functions

# Source the main script functions without running main()
# We do this by extracting functions only
setup_test_env() {
    export GLUETUN_CONTAINER="test-gluetun"
    export CONFIG_FILE="/tmp/test-sites.conf"
    export LOG_FILE="/tmp/test-monitor.log"
    export CHECK_INTERVAL="30"
    export TIMEOUT="10"
    export FAIL_THRESHOLD="2"
    export HEALTHY_WAIT_TIMEOUT="120"
    export DEPENDENT_CONTAINERS="auto"
}

# Create a minimal sites.conf for testing
create_test_sites_conf() {
    cat > "$CONFIG_FILE" << 'EOF'
# Test sites
https://www.google.com
https://cloudflare.com
EOF
}

cleanup_test_env() {
    rm -f "$CONFIG_FILE" "$LOG_FILE"
}

# Mock docker command for testing
mock_docker() {
    # Create a mock docker script
    cat > /tmp/mock_docker << 'EOF'
#!/bin/bash
echo "mock_docker called with: $*" >&2
case "$1" in
    ps)
        echo "test-gluetun"
        ;;
    inspect)
        if [[ "$*" == *"Health.Status"* ]]; then
            echo "healthy"
        elif [[ "$*" == *"NetworkMode"* ]]; then
            echo "container:test-gluetun"
        elif [[ "$*" == *"Id"* ]]; then
            echo "abc123def456"
        elif [[ "$*" == *"Name"* ]]; then
            echo "/test-container"
        fi
        ;;
    exec)
        # Simulate successful wget
        exit 0
        ;;
    logs)
        echo "[ip getter] Public IP address is 1.2.3.4 (United States, New York, NYC - source: ipinfo)"
        ;;
    restart)
        echo "Restarted"
        ;;
esac
EOF
    chmod +x /tmp/mock_docker
}
