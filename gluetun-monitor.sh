#!/bin/bash

# Gluetun VPN Monitor
# Tests multiple sites and restarts gluetun + dependent containers on failure

set -euo pipefail

# Configuration
CONFIG_FILE="${CONFIG_FILE:-/config/sites.conf}"
LOG_FILE="${LOG_FILE:-/logs/gluetun-monitor.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
TIMEOUT="${TIMEOUT:-10}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"
GLUETUN_CONTAINER="${GLUETUN_CONTAINER:-gluetun}"
HEALTHY_WAIT_TIMEOUT="${HEALTHY_WAIT_TIMEOUT:-120}"

# Dependent containers - set to "auto" to discover dynamically, or comma-separated list
DEPENDENT_CONTAINERS="${DEPENDENT_CONTAINERS:-auto}"

# Consecutive failure counter
declare -A site_failures

# Track site count for change detection
LAST_SITE_COUNT=""

log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    # Write to stderr (so command substitution doesn't capture it) and log file
    echo "[$timestamp] [$level] $message" | tee -a "$LOG_FILE" >&2
}

log_endpoint_info() {
    local status="$1"
    local reason="${2:-}"

    # Get the most recent "ip getter" line which contains all the info we need
    # Format: [ip getter] Public IP address is 31.40.215.70 (Switzerland, Zurich, Zürich - source: ipinfo)
    local ip_getter_line
    ip_getter_line=$(docker logs "$GLUETUN_CONTAINER" 2>&1 | grep -i "ip getter.*Public IP address" | tail -1)

    local public_ip="unknown"
    local country="unknown"
    local city="unknown"

    if [[ -n "$ip_getter_line" ]]; then
        # Extract IP address
        public_ip=$(echo "$ip_getter_line" | grep -oP 'Public IP address is \K[0-9.]+' || echo "unknown")

        # Extract location info from parentheses: (Country, City, Region - source: xxx)
        local location
        location=$(echo "$ip_getter_line" | grep -oP '\(\K[^)]+' | sed 's/ - source:.*//')
        if [[ -n "$location" ]]; then
            country=$(echo "$location" | cut -d',' -f1 | xargs)
            city=$(echo "$location" | cut -d',' -f2 | xargs)
        fi
    fi

    # Also get the wireguard server IP
    local wg_server
    wg_server=$(docker logs "$GLUETUN_CONTAINER" 2>&1 | grep -i "wireguard.*Connecting to" | tail -1 | grep -oP 'Connecting to \K[0-9.]+' || echo "unknown")

    log "ENDPOINT" "Status: $status | IP: $public_ip | Country: $country | City: $city | VPN Server: $wg_server | Reason: $reason"
}

get_gluetun_health() {
    local health
    health=$(docker inspect --format='{{.State.Health.Status}}' "$GLUETUN_CONTAINER" 2>/dev/null || echo "unknown")
    echo "$health"
}

discover_dependent_containers() {
    # Find all containers using network_mode: container:<GLUETUN_CONTAINER>
    local found_containers=()

    # Get the gluetun container ID (NetworkMode can use ID or name)
    local gluetun_id
    gluetun_id=$(docker inspect --format='{{.Id}}' "$GLUETUN_CONTAINER" 2>/dev/null)
    local gluetun_short_id="${gluetun_id:0:12}"

    log "DEBUG" "Discovering containers using gluetun network (name: $GLUETUN_CONTAINER, id: $gluetun_short_id)"

    # Get all running container IDs
    local all_containers
    all_containers=$(docker ps -q 2>/dev/null)

    if [[ -z "$all_containers" ]]; then
        log "WARN" "No running containers found"
        echo ""
        return
    fi

    # Check each container's NetworkMode
    local container_name network_mode
    for container_id in $all_containers; do
        container_name=$(docker inspect --format='{{.Name}}' "$container_id" 2>/dev/null | sed 's/^\///')
        network_mode=$(docker inspect --format='{{.HostConfig.NetworkMode}}' "$container_id" 2>/dev/null)

        # Skip the gluetun container itself
        if [[ "$container_name" == "$GLUETUN_CONTAINER" ]]; then
            continue
        fi

        # Check if this container uses gluetun's network
        # NetworkMode can be: container:<name> OR container:<id>
        if [[ "$network_mode" == "container:${GLUETUN_CONTAINER}" ]] || \
           [[ "$network_mode" == "container:${gluetun_id}" ]] || \
           [[ "$network_mode" == "container:${gluetun_short_id}"* ]]; then
            found_containers+=("$container_name")
            log "DEBUG" "Found dependent container: $container_name"
        fi
    done

    # Return comma-separated list
    local result
    result=$(IFS=','; echo "${found_containers[*]}")
    echo "$result"
}

get_dependent_containers() {
    local discovered
    # If set to "auto", discover dynamically
    if [[ "$DEPENDENT_CONTAINERS" == "auto" ]]; then
        discovered=$(discover_dependent_containers)
        if [[ -z "$discovered" ]]; then
            log "WARN" "No dependent containers discovered"
        else
            log "INFO" "Discovered dependent containers: $discovered"
        fi
        echo "$discovered"
    else
        # Use the configured list
        echo "$DEPENDENT_CONTAINERS"
    fi
}

wait_for_gluetun_healthy() {
    local max_wait="${1:-$HEALTHY_WAIT_TIMEOUT}"
    local waited=0

    log "INFO" "Waiting for $GLUETUN_CONTAINER to become healthy (max ${max_wait}s)..."

    local health
    while [[ $waited -lt $max_wait ]]; do
        health=$(get_gluetun_health)
        if [[ "$health" == "healthy" ]]; then
            log "INFO" "$GLUETUN_CONTAINER is healthy after ${waited}s"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
        log "DEBUG" "Waiting... ($waited/${max_wait}s) - current status: $health"
    done

    log "ERROR" "$GLUETUN_CONTAINER did not become healthy within ${max_wait}s"
    return 1
}

wait_for_dns_ready() {
    # Wait for gluetun's DNS to be fully operational
    local max_wait="${1:-30}"
    local waited=0

    log "INFO" "Waiting for DNS to stabilize..."

    while [[ $waited -lt $max_wait ]]; do
        # Try to resolve google.com through gluetun
        if docker exec "$GLUETUN_CONTAINER" nslookup google.com 127.0.0.1 >/dev/null 2>&1; then
            # Also try a quick ping to verify connectivity
            if docker exec "$GLUETUN_CONTAINER" wget --spider --timeout=5 --tries=1 -q "https://1.1.1.1" >/dev/null 2>&1; then
                log "INFO" "DNS and connectivity verified after ${waited}s"
                return 0
            fi
        fi
        sleep 2
        waited=$((waited + 2))
        log "DEBUG" "Waiting for DNS... ($waited/${max_wait}s)"
    done

    log "WARN" "DNS stabilization timeout after ${max_wait}s - proceeding anyway"
    return 1
}

decode_wget_exit_code() {
    local code="$1"
    case $code in
        0) echo "Success" ;;
        1) echo "Generic error" ;;
        2) echo "Parse error" ;;
        3) echo "File I/O error" ;;
        4) echo "Network failure (DNS or connection)" ;;
        5) echo "SSL verification failure" ;;
        6) echo "Authentication required" ;;
        7) echo "Protocol error" ;;
        8) echo "Server error (HTTP 4xx/5xx)" ;;
        *) echo "Unknown error (code $code)" ;;
    esac
}

test_site_async() {
    # Runs in background, writes result to temp file
    local site="$1"
    local result_file="$2"
    local start_time end_time duration
    start_time=$(date +%s%3N)

    # Use wget --spider -S (server response) to get HTTP status code
    local response
    response=$(docker exec "$GLUETUN_CONTAINER" wget --spider -S --timeout="$TIMEOUT" --tries=1 -q "$site" 2>&1)
    local exit_code=$?

    end_time=$(date +%s%3N)
    duration=$((end_time - start_time))

    # Extract HTTP status code from response (e.g., "HTTP/1.1 200 OK" or "HTTP/1.1 403 Forbidden")
    local http_code
    http_code=$(echo "$response" | grep -oP 'HTTP/[0-9.]+ \K[0-9]+' | tail -1)
    [[ -z "$http_code" ]] && http_code="N/A"

    # Determine if this is a real failure (connectivity issue) or just a site error
    # Exit codes that mean VPN IS working (site responded):
    #   6 = Auth required (site responded)
    #   8 = HTTP 4xx/5xx (site responded)
    # Exit codes that mean VPN may be broken:
    #   4 = Network failure (DNS or connection)
    #   5 = SSL verification failure
    #   Others = Various failures

    if [[ $exit_code -eq 0 ]]; then
        echo "PASS:${duration}:HTTP ${http_code}" > "$result_file"
    elif [[ $exit_code -eq 6 ]] || [[ $exit_code -eq 8 ]]; then
        # Site responded with an error - VPN is working
        echo "PASS:${duration}:HTTP ${http_code} (VPN working)" > "$result_file"
    else
        # Real connectivity failure
        local error_reason
        error_reason=$(decode_wget_exit_code $exit_code)
        echo "FAIL:${duration}:${error_reason}" > "$result_file"
    fi
}

test_all_sites() {
    local sites_file="$1"
    local failed_sites=()
    local passed_sites=()
    local pids=()
    local sites=()

    # Create temp directory for results (cleaned up after)
    local temp_dir
    temp_dir=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf '$temp_dir'" RETURN

    # Verify config file exists and log its state
    if [[ ! -f "$sites_file" ]]; then
        log "ERROR" "Sites config file not found: $sites_file"
        return 1
    fi

    local site_count
    site_count=$(grep -cv '^#\|^[[:space:]]*$' "$sites_file")

    # Only log site count on first run or when it changes
    if [[ "$LAST_SITE_COUNT" != "$site_count" ]]; then
        if [[ -z "$LAST_SITE_COUNT" ]]; then
            log "INFO" "Loaded $site_count sites from $sites_file"
        else
            log "INFO" "Site count changed in $sites_file from $LAST_SITE_COUNT to $site_count"
        fi
        LAST_SITE_COUNT="$site_count"
    fi

    # Read all sites and launch parallel tests
    local safe_name result_file
    while IFS= read -r site || [[ -n "$site" ]]; do
        # Skip empty lines and comments
        [[ -z "$site" || "$site" =~ ^[[:space:]]*# ]] && continue

        # Trim whitespace
        site=$(echo "$site" | xargs)
        [[ -z "$site" ]] && continue

        sites+=("$site")

        # Create safe filename from URL
        safe_name=$(echo "$site" | md5sum | cut -d' ' -f1)
        result_file="${temp_dir}/${safe_name}"

        # Launch test in background
        test_site_async "$site" "$result_file" &
        pids+=($!)

    done < "$sites_file"

    # Wait for all parallel tests to complete (bounded by TIMEOUT)
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done

    # Collect results
    local result status remainder duration reason current_failures remaining
    for site in "${sites[@]}"; do
        safe_name=$(echo "$site" | md5sum | cut -d' ' -f1)
        result_file="${temp_dir}/${safe_name}"

        if [[ -f "$result_file" ]]; then
            result=$(cat "$result_file")
            # Parse format: STATUS:DURATION:REASON
            status="${result%%:*}"
            remainder="${result#*:}"
            duration="${remainder%%:*}"
            reason="${remainder#*:}"

            if [[ "$status" == "PASS" ]]; then
                passed_sites+=("$site")
                site_failures["$site"]=0
                log "DEBUG" "Site $site passed (${duration}ms)"
            else
                # Increment failure counter
                site_failures["$site"]=$((${site_failures["$site"]:-0} + 1))
                current_failures=${site_failures["$site"]}
                remaining=$((FAIL_THRESHOLD - current_failures))

                if [[ $current_failures -ge $FAIL_THRESHOLD ]]; then
                    failed_sites+=("$site")
                    log "WARN" "Site $site failed $current_failures consecutive times - THRESHOLD REACHED - ${reason}"
                else
                    log "DEBUG" "Site $site failed ($current_failures/$FAIL_THRESHOLD) - $remaining more to trigger restart - ${reason}"
                fi
            fi
        else
            # Result file missing - treat as failure
            site_failures["$site"]=$((${site_failures["$site"]:-0} + 1))
            log "DEBUG" "Site $site test result missing"
        fi
    done

    # Temp dir cleaned up by trap

    # Calculate totals
    local total_sites=${#sites[@]}
    local passed_count=${#passed_sites[@]}
    local failed_count=${#failed_sites[@]}
    local pending_count=$((total_sites - passed_count - failed_count))

    # Log summary (only when there's something to report)
    if [[ $failed_count -gt 0 ]]; then
        log "ERROR" "Failed sites (exceeded threshold): ${failed_sites[*]}"
        log "INFO" "Summary: $passed_count passed, $failed_count failed, $pending_count pending"
        return 1
    elif [[ $pending_count -gt 0 ]]; then
        log "DEBUG" "Summary: $passed_count/$total_sites passed, $pending_count pending failure"
    fi

    return 0
}

restart_gluetun() {
    log "INFO" "Restarting $GLUETUN_CONTAINER to force new endpoint..."

    # Log current endpoint before restart
    log_endpoint_info "FAILING" "Site connectivity test failed"

    # Restart gluetun
    docker restart "$GLUETUN_CONTAINER"

    # Wait for it to become healthy
    if wait_for_gluetun_healthy "$HEALTHY_WAIT_TIMEOUT"; then
        # Wait for DNS to stabilize before testing
        wait_for_dns_ready 30

        # Log new endpoint info
        log_endpoint_info "NEW" "After restart"
        return 0
    else
        log "ERROR" "Gluetun failed to become healthy after restart"
        return 1
    fi
}

restart_dependent_containers() {
    log "INFO" "Discovering and restarting dependent containers..."

    local containers_to_restart
    containers_to_restart=$(get_dependent_containers)

    if [[ -z "$containers_to_restart" ]]; then
        log "WARN" "No dependent containers to restart"
        return
    fi

    IFS=',' read -ra containers <<< "$containers_to_restart"

    for container in "${containers[@]}"; do
        container=$(echo "$container" | xargs)  # Trim whitespace

        if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
            log "INFO" "Restarting $container..."
            docker restart "$container"

            # Wait a moment between restarts
            sleep 2

            # Check if it started
            if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
                log "INFO" "$container restarted successfully"
            else
                log "ERROR" "$container failed to restart"
            fi
        else
            log "WARN" "Container $container not found, skipping"
        fi
    done

    log "INFO" "Dependent container restart complete"
}

handle_failure() {
    log "WARN" "Health check failed, initiating recovery..."

    if restart_gluetun; then
        # Verify connectivity before restarting dependents
        log "INFO" "Verifying connectivity before restarting dependents..."
        if test_all_sites "$CONFIG_FILE"; then
            log "INFO" "Connectivity verified, restarting dependent containers..."
            restart_dependent_containers

            # Reset failure counters after successful recovery
            for key in "${!site_failures[@]}"; do
                site_failures["$key"]=0
            done

            log "INFO" "Recovery complete"
        else
            log "WARN" "Connectivity still failing after restart, trying another endpoint..."
            # Reset failure counters to give the next attempt a fresh start
            for key in "${!site_failures[@]}"; do
                site_failures["$key"]=0
            done
            # Don't restart dependents - let the next check cycle handle it
        fi
    else
        log "ERROR" "Recovery failed - manual intervention may be required"
    fi
}

check_prerequisites() {
    # Check if config file exists
    if [[ ! -f "$CONFIG_FILE" ]]; then
        log "ERROR" "Config file not found: $CONFIG_FILE"
        exit 1
    fi

    # Check if docker is accessible
    if ! docker info >/dev/null 2>&1; then
        log "ERROR" "Cannot connect to Docker daemon"
        exit 1
    fi

    # Check if gluetun container exists
    if ! docker ps -a --format '{{.Names}}' | grep -q "^${GLUETUN_CONTAINER}$"; then
        log "ERROR" "Gluetun container '$GLUETUN_CONTAINER' not found"
        exit 1
    fi

    # Ensure log directory exists
    mkdir -p "$(dirname "$LOG_FILE")"

    log "INFO" "Prerequisites check passed"
}

main() {
    log "INFO" "Gluetun Monitor starting..."
    log "INFO" "Config: CHECK_INTERVAL=${CHECK_INTERVAL}s, TIMEOUT=${TIMEOUT}s, FAIL_THRESHOLD=${FAIL_THRESHOLD}"
    log "INFO" "Monitoring container: $GLUETUN_CONTAINER"

    check_prerequisites

    # Show dependent containers at startup (for visibility)
    if [[ "$DEPENDENT_CONTAINERS" == "auto" ]]; then
        local initial_dependents
        initial_dependents=$(discover_dependent_containers)
        if [[ -n "$initial_dependents" ]]; then
            log "INFO" "Dependent containers (auto-discovery): $initial_dependents"
        else
            log "INFO" "Dependent containers: auto-discovery enabled (none found currently)"
        fi
    else
        log "INFO" "Dependent containers (manual): $DEPENDENT_CONTAINERS"
    fi

    # Log initial endpoint info
    log_endpoint_info "STARTUP" "Monitor starting"

    local gluetun_health
    while true; do
        log "CHECK" "Start"

        # Check if gluetun is running
        if ! docker ps --format '{{.Names}}' | grep -q "^${GLUETUN_CONTAINER}$"; then
            log "ERROR" "Gluetun container is not running!"
            log "CHECK" "End - Sleeping ${CHECK_INTERVAL}s"
            sleep "$CHECK_INTERVAL"
            continue
        fi

        # Check gluetun health status first
        gluetun_health=$(get_gluetun_health)
        if [[ "$gluetun_health" != "healthy" ]]; then
            log "WARN" "Gluetun health status: $gluetun_health"
        fi

        # Test all sites
        if ! test_all_sites "$CONFIG_FILE"; then
            handle_failure
        fi

        log "CHECK" "End - Sleeping ${CHECK_INTERVAL}s"
        sleep "$CHECK_INTERVAL"
    done
}

# Handle signals gracefully
trap 'log "INFO" "Received shutdown signal, exiting..."; exit 0' SIGTERM SIGINT

main "$@"
