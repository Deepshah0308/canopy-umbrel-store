#!/bin/sh
# Starts an optional YAMS app only once it's switched on in the dashboard.
#
#   gate.sh <name> <the app's normal command...>
#
# While the app is off this just sleeps (a few hundred KB of memory), so an extra you
# never turn on costs nothing but disk space. The dashboard switches it on by creating
# /yams-extras/<name>; it switches it off by removing that file and restarting the
# container, which brings it back here.
name="$1"
shift

if [ ! -f "/yams-extras/$name" ]; then
	# As the container's main process, the shell would ignore Docker's stop signal and
	# make every stop wait for the timeout. Exit promptly instead.
	trap 'exit 0' TERM INT
	echo "[yams] $name is switched off. Turn it on in the YAMS dashboard under Extras."
	while [ ! -f "/yams-extras/$name" ]; do
		sleep 5 &
		wait $!
	done
	trap - TERM INT
fi

echo "[yams] Starting $name"
exec "$@"
