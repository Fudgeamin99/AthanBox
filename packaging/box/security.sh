#!/bin/sh
# Kept for boxes that were told to run it (0.5.0): everything that needs root is in system-setup.sh now,
# which also lets later versions set themselves up after each update.
#     sudo sh packaging/box/security.sh
exec sh "$(dirname "$0")/system-setup.sh" "$@"
