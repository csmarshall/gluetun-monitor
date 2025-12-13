#!/usr/bin/env bats

# Unit tests for gluetun-monitor functions

setup() {
    # Source test helpers
    source tests/test_helper.bash
    setup_test_env

    # Extract functions from main script (without running main)
    # We source the script but override main to do nothing
    eval "$(sed 's/^main "\$@"$/# main disabled for testing/' gluetun-monitor.sh)"
}

teardown() {
    cleanup_test_env
}

@test "decode_wget_exit_code returns correct message for exit 0" {
    result=$(decode_wget_exit_code 0)
    [ "$result" = "Success" ]
}

@test "decode_wget_exit_code returns correct message for exit 4" {
    result=$(decode_wget_exit_code 4)
    [ "$result" = "Network failure (DNS or connection)" ]
}

@test "decode_wget_exit_code returns correct message for exit 5" {
    result=$(decode_wget_exit_code 5)
    [ "$result" = "SSL verification failure" ]
}

@test "decode_wget_exit_code returns correct message for exit 6" {
    result=$(decode_wget_exit_code 6)
    [ "$result" = "Authentication required" ]
}

@test "decode_wget_exit_code returns correct message for exit 8" {
    result=$(decode_wget_exit_code 8)
    [ "$result" = "Server error (HTTP 4xx/5xx)" ]
}

@test "decode_wget_exit_code handles unknown codes" {
    result=$(decode_wget_exit_code 99)
    [ "$result" = "Unknown error (code 99)" ]
}

@test "log function writes to stderr" {
    # Capture stderr
    result=$(log "INFO" "Test message" 2>&1)
    [[ "$result" == *"[INFO] Test message"* ]]
}

@test "log function includes timestamp" {
    result=$(log "INFO" "Test" 2>&1)
    # Should contain date format YYYY-MM-DD
    [[ "$result" =~ \[20[0-9]{2}-[0-9]{2}-[0-9]{2} ]]
}

@test "sites.conf.example exists and has valid entries" {
    [ -f "sites.conf.example" ]
    # Should have at least google.com
    grep -q "google.com" sites.conf.example
}

@test "docker-compose.yml.example exists and has required fields" {
    [ -f "docker-compose.yml.example" ]
    grep -q "GLUETUN_CONTAINER" docker-compose.yml.example
    grep -q "docker.sock" docker-compose.yml.example
}

@test "script has no shellcheck errors" {
    run shellcheck gluetun-monitor.sh
    [ "$status" -eq 0 ]
}

@test "FAIL_THRESHOLD default is 2" {
    unset FAIL_THRESHOLD
    source <(grep "^FAIL_THRESHOLD=" gluetun-monitor.sh)
    [ "$FAIL_THRESHOLD" = "2" ]
}

@test "CHECK_INTERVAL default is 30" {
    unset CHECK_INTERVAL
    source <(grep "^CHECK_INTERVAL=" gluetun-monitor.sh)
    [ "$CHECK_INTERVAL" = "30" ]
}

@test "DEPENDENT_CONTAINERS default is auto" {
    unset DEPENDENT_CONTAINERS
    source <(grep "^DEPENDENT_CONTAINERS=" gluetun-monitor.sh)
    [ "$DEPENDENT_CONTAINERS" = "auto" ]
}
