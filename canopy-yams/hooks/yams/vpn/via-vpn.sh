#!/bin/sh
# Starts FlareSolverr, sending its traffic through the VPN's proxy when "Search through
# the VPN" is on in the YAMS dashboard (the dashboard restarts FlareSolverr after a change).
if [ -f /yams-state/prowlarr-vpn ]; then
	export PROXY_URL="http://canopy-yams_gluetun_1:8888"
	echo "[yams] FlareSolverr goes through the VPN"
fi
exec "$@"
