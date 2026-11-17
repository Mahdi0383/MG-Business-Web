#!/bin/sh
set -eu
cd /opt/mg-business
/usr/bin/python3 deployment_preflight.py --production
exec /usr/bin/python3 server.py
