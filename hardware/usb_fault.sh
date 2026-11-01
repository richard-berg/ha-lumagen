#!/usr/bin/env bash
# USB faults for the Lumagen's serial adapter, on the machine whose kernel
# owns it (the VM or host it is passed through to). Needs root.
#
#   usb_fault.sh list                        matching devices
#   usb_fault.sh unplug SECONDS              logical unplug (USB deauthorize)
#   usb_fault.sh unbind SECONDS              detach the serial driver only
#   usb_fault.sh flap COUNT DOWN UP          COUNT unplugs, DOWN s out, UP s in
#   usb_fault.sh chaos HOURS                 random unplugs and unbinds
#
# The device is found by USB serial number (USB_SERIAL), or failing that by
# vendor:product (USB_ID, default 0403:6001, FTDI FT232R).
#
# MARKS=path appends each fault to a marks file in the format
# `lumagen_probe.py monitor --marks` reads, so its outages count as explained.
# Point it at a directory the monitor can see.
set -euo pipefail

USB_ID=${USB_ID:-0403:6001}
USB_SERIAL=${USB_SERIAL:-}
MARKS=${MARKS:-}

log() { printf '%s %s\n' "$(date -u +%FT%T.%3NZ)" "$*"; }

mark() {  # label seconds
    [ -n "$MARKS" ] || return 0
    printf '{"t": "%s", "label": "%s", "seconds": %s}\n' \
        "$(date -u +%FT%T.%3N+00:00)" "$1" "$2" >> "$MARKS"
}

find_device() {
    local dev vendor product serial
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idVendor" ] || continue
        vendor=$(cat "$dev/idVendor"); product=$(cat "$dev/idProduct")
        serial=$(cat "$dev/serial" 2>/dev/null || true)
        if [ -n "$USB_SERIAL" ]; then
            [ "$serial" = "$USB_SERIAL" ] || continue
        else
            [ "$vendor:$product" = "$USB_ID" ] || continue
        fi
        echo "$dev"
    done
}

device() {
    local found count
    found=$(find_device)
    count=$(printf '%s' "$found" | grep -c . || true)
    if [ "$count" != 1 ]; then
        echo "Expected one matching USB device, found $count; set USB_SERIAL" >&2
        exit 1
    fi
    echo "$found"
}

tty_of() {  # the ttyUSB name under a USB device, if bound
    local tty
    tty=$(find "$1"/ -maxdepth 3 -name 'ttyUSB*' -printf '%f\n' 2>/dev/null | head -1)
    echo "${tty:-none}"
}

unplug() {  # device seconds
    log "unplug $(basename "$1") ($(tty_of "$1")) for ${2}s"
    mark "usb unplug" "$2"
    echo 0 > "$1/authorized"
    sleep "$2"
    echo 1 > "$1/authorized"
    sleep 2
    log "replugged as $(tty_of "$1")"
}

unbind() {  # device seconds
    local intf
    intf=$(basename "$(dirname "$(find "$1"/ -maxdepth 2 -name 'ttyUSB*' | head -1)")")
    if [ -z "$intf" ] || [ "$intf" = . ]; then
        echo "No serial interface bound under $1" >&2
        return 1
    fi
    log "unbind $intf ($(tty_of "$1")) for ${2}s"
    mark "usb unbind" "$2"
    echo "$intf" > /sys/bus/usb/drivers/ftdi_sio/unbind
    sleep "$2"
    echo "$intf" > /sys/bus/usb/drivers/ftdi_sio/bind
    sleep 2
    log "rebound as $(tty_of "$1")"
}

case "${1:-}" in
    list | unplug | unbind | flap | chaos)
        [ "$(id -u)" = 0 ] || { echo "Run as root" >&2; exit 1; } ;;
esac

case "${1:-}" in
    list)
        for dev in $(find_device); do
            echo "$dev $(cat "$dev/idVendor"):$(cat "$dev/idProduct")" \
                "serial=$(cat "$dev/serial" 2>/dev/null) tty=$(tty_of "$dev")"
        done
        ;;
    unplug) unplug "$(device)" "${2:?seconds}" ;;
    unbind) unbind "$(device)" "${2:?seconds}" ;;
    flap)
        dev=$(device)
        for _ in $(seq "${2:?count}"); do
            unplug "$dev" "${3:?down seconds}"
            sleep "${4:?up seconds}"
        done
        ;;
    chaos)
        dev=$(device)
        end=$(( $(date +%s) + ${2:?hours} * 3600 ))
        while [ "$(date +%s)" -lt "$end" ]; do
            sleep $(( 300 + RANDOM % 3300 ))
            hold=$(( 1 + RANDOM % 120 ))
            case $(( RANDOM % 4 )) in
                0) unbind "$dev" "$hold" ;;
                1) for _ in 1 2 3 4 5; do unplug "$dev" 1; sleep 1; done ;;
                *) unplug "$dev" "$hold" ;;
            esac
        done
        ;;
    *)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac
