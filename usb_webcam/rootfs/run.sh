#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starte USB Webcam ..."
exec python3 -u /usr/local/bin/usb_webcam.py
